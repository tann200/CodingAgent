"""Regression tests for TASK-02: LangGraph history replacement, termination, cancellation, delete_file verification, and RollbackManager.commit_step."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.orchestration.graph.canonical_routing import (
    route_after_agent_loop,
    route_after_verify,
)
from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node
from src.core.orchestration.graph.nodes.verification_node import verification_node
from src.core.orchestration.graph.state import AgentState, ReplaceList
from src.core.orchestration.rollback_manager import RollbackManager


def _make_orch_mock(tmp_path):
    adapter = MagicMock()
    adapter.model = "claude-sonnet-4.6"
    adapter.provider = SimpleNamespace(name="mock")

    registry = MagicMock()
    registry.tools = {}

    msg_mgr = MagicMock()
    msg_mgr.system_prompt = "You are a helpful coding assistant."

    orch = MagicMock()
    orch.working_dir = str(tmp_path)
    orch.adapter = adapter
    orch.tool_registry = registry
    orch.msg_mgr = msg_mgr
    orch.event_bus = MagicMock()
    orch.plan_mode = None
    orch.cancel_event = None
    orch._usage_buffer = {}
    orch._session_modified_files = set()
    orch.execute_tool = AsyncMock(return_value={"status": "ok"})
    return orch


def _wrap_resp(content: str = "", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


class TestTask02StateAndHistory:
    def test_agent_state_includes_verified_file_hashes(self):
        hints = get_type_hints(AgentState, include_extras=True)
        assert "_verified_file_hashes" in hints

    @pytest.mark.asyncio
    async def test_frontier_loop_returns_replace_list_history(self, tmp_path):
        orch = _make_orch_mock(tmp_path)
        state = {
            "task": "Say done",
            "working_dir": str(tmp_path),
            "history": [{"role": "user", "content": "Say done"}],
            "max_turns": 4,
            "max_tool_calls": 10,
        }
        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(return_value=_wrap_resp("Done.")),
            ),
        ):
            res = await frontier_loop_node(state, config={})

        assert isinstance(res["history"], ReplaceList)
        assert res["run"]["outcome"] == "complete"
        assert res["replan_required"] is None
        assert res["action_failed"] is False


class TestTask02LoopTerminationAndRouting:
    @pytest.mark.asyncio
    async def test_tool_budget_exhaustion_does_not_emit_continue(self, tmp_path):
        orch = _make_orch_mock(tmp_path)
        state = {
            "task": "Read a.py",
            "working_dir": str(tmp_path),
            "history": [{"role": "user", "content": "Read a.py"}],
            "max_turns": 5,
            "max_tool_calls": 1,
            "runtime": {"policy": {"tool_limit": 30, "turn_limit": 10}},
        }
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
        }
        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(return_value=_wrap_resp("", [tool_call])),
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.execute_raw_tool",
                new=AsyncMock(return_value={"status": "ok", "content": "x = 1"}),
            ),
        ):
            res = await frontier_loop_node(state, config={})

        assert res["tool_call_count"] >= 1
        assert res["run"]["outcome"] == "failed"
        assert res["run"]["status"] == "failed"
        assert route_after_agent_loop({**state, **res}) == "finalize"

    @pytest.mark.asyncio
    async def test_llm_exception_sets_failed_outcome_and_avoids_continue_loop(
        self, tmp_path
    ):
        orch = _make_orch_mock(tmp_path)
        state = {
            "task": "Inspect repo",
            "working_dir": str(tmp_path),
            "history": [{"role": "user", "content": "Inspect repo"}],
            "max_turns": 5,
            "max_tool_calls": 10,
        }
        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(side_effect=RuntimeError("provider offline")),
            ),
        ):
            res = await frontier_loop_node(state, config={})

        assert res["run"]["outcome"] == "failed"
        assert res["run"]["status"] == "failed"
        assert res["last_result"]["ok"] is False
        assert "provider offline" in res["last_result"]["error"]
        assert route_after_agent_loop({**state, **res}) == "finalize"

    @pytest.mark.asyncio
    async def test_cancel_event_stops_frontier_loop_and_routes_to_finalize(
        self, tmp_path
    ):
        cancel_ev = threading.Event()
        cancel_ev.set()
        orch = _make_orch_mock(tmp_path)
        orch.cancel_event = cancel_ev
        state = {
            "task": "Cancelled task",
            "working_dir": str(tmp_path),
            "history": [{"role": "user", "content": "Cancelled task"}],
            "cancel_event": cancel_ev,
            "max_turns": 4,
            "max_tool_calls": 10,
        }
        mock_call = AsyncMock(return_value=_wrap_resp("should not run"))
        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=mock_call,
            ),
        ):
            res = await frontier_loop_node(state, config={})

        mock_call.assert_not_called()
        assert res["run"]["outcome"] == "cancelled"
        assert res["run"]["status"] == "cancelled"
        assert route_after_agent_loop({**state, **res}) == "finalize"

    @pytest.mark.asyncio
    async def test_verification_node_cancellation_routes_to_finalize_and_history_is_replace_list(
        self, tmp_path
    ):
        cancel_ev = threading.Event()
        cancel_ev.set()
        state = {
            "cancel_event": cancel_ev,
            "modified_files": ["foo.py"],
            "working_dir": str(tmp_path),
            "run": {"status": "running", "outcome": "complete", "recoveries": 0},
            "history": [{"role": "user", "content": "x"}],
        }
        with patch(
            "src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator",
            return_value=None,
        ):
            res = await verification_node(state, config={})

        assert res["run"]["status"] == "cancelled"
        assert res["run"]["outcome"] == "cancelled"
        assert route_after_verify({**state, **res}) == "finalize"

        # Now verify failed verification wraps history feedback in ReplaceList
        mod = tmp_path / "broken.py"
        mod.write_text("x = (\n", encoding="utf-8")
        fail_state = {
            "modified_files": [str(mod)],
            "working_dir": str(tmp_path),
            "last_tool_name": "write_file",
            "last_result": {"status": "ok", "path": str(mod)},
            "run": {"status": "running", "outcome": "complete", "recoveries": 0},
            "runtime": {"policy": {"verification_mode": "targeted", "recovery_limit": 2}},
            "history": [{"role": "user", "content": "write broken.py"}],
        }
        with patch(
            "src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator",
            return_value=None,
        ):
            fail_res = await verification_node(fail_state, config={})

        assert isinstance(fail_res.get("history"), ReplaceList)


class TestTask02DeleteFileAndCommitStep:
    @pytest.mark.asyncio
    async def test_delete_file_verification_passes_without_error(self, tmp_path):
        deleted_path = tmp_path / "obsolete.py"
        assert not deleted_path.exists()
        state = {
            "working_dir": str(tmp_path),
            "modified_files": [],
            "last_tool_name": "delete_file",
            "last_result": {"status": "ok", "path": str(deleted_path)},
            "run": {"status": "running", "outcome": "complete", "recoveries": 0},
            "runtime": {"policy": {"verification_mode": "targeted", "recovery_limit": 2}},
        }
        with patch(
            "src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator",
            return_value=None,
        ):
            res = await verification_node(state, config={})

        assert res["verification_passed"] is True
        assert res["run"]["outcome"] == "complete"
        assert res["verification"]["passed"] is True
        assert route_after_verify({**state, **res}) == "finalize"

    def test_rollback_manager_commit_step_removes_mapped_snapshot(self, tmp_path):
        target = tmp_path / "sample.py"
        target.write_text("v1\n", encoding="utf-8")

        rm = RollbackManager(str(tmp_path))
        snap_id = rm.snapshot_files([])
        rm.append_to_snapshot(snap_id, "sample.py")
        rm._step_to_snapshot["step-1"] = snap_id
        snap_file = rm.snapshot_dir / f"{snap_id}.json"
        assert snap_id in rm.snapshots
        assert snap_file.exists()

        rm.commit_step("step-1")
        assert snap_id not in rm.snapshots
        assert not snap_file.exists()
        assert rm.current_snapshot is None

    def test_rollback_manager_commit_step_accepts_direct_snapshot_id(self, tmp_path):
        target = tmp_path / "sample2.py"
        target.write_text("v1\n", encoding="utf-8")

        rm = RollbackManager(str(tmp_path))
        snap_id = rm.snapshot_files(["sample2.py"])
        snap_file = rm.snapshot_dir / f"{snap_id}.json"
        assert snap_file.exists()

        rm.commit_step(snap_id)
        assert snap_id not in rm.snapshots
        assert not snap_file.exists()
        assert rm.current_snapshot is None
