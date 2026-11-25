"""Regression tests for TASK-08: PlanMode, approval digest/preview, JSON checkpointing, MessageManager sync, config reload, and completion_contract."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.orchestration.approval_gate import AsyncGate
from src.core.orchestration.graph.checkpoint_saver import (
    JsonlCheckpointSaver,
    rehydrate_initial_state,
)
from src.core.orchestration.graph.completion_contract import (
    TaskType,
    evaluate_completion_contract,
)
from src.core.orchestration.graph.nodes.approval_node import _approval_node_impl
from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node
from src.core.orchestration.graph.policy import compute_action_digest
from src.core.orchestration.inference_loop_responses import (
    _sync_msg_mgr_with_final_history,
)
from src.core.orchestration.message_manager import MessageManager
from src.core.orchestration.orchestrator_config_reload import _on_config_reloaded
from src.core.orchestration.plan_mode import PlanMode
from src.tools.permission_context import ToolPermissionContext


class TestTask08PlanModeAndApprovalNode:
    def test_plan_mode_blocked_tools_covers_mutating_primitives(self):
        for name in (
            "edit_file",
            "edit_file_atomic",
            "multiedit",
            "rename_file",
            "bash",
            "git_commit",
            "write_file",
            "delete_file",
        ):
            assert name in PlanMode.BLOCKED_TOOLS

    @pytest.mark.asyncio
    async def test_multi_tool_batch_plan_mode_suspension_leaves_no_orphan_tool_calls(
        self, tmp_path
    ):
        orch = MagicMock()
        orch.working_dir = str(tmp_path)
        orch.adapter = SimpleNamespace(model="claude-sonnet-4.6", provider=SimpleNamespace(name="mock"))
        orch.tool_registry = MagicMock(tools={})
        orch.msg_mgr = MagicMock(system_prompt="sys")
        orch.event_bus = MagicMock()
        orch.plan_mode = PlanMode(orch)
        orch.plan_mode.enable()
        orch._plan_mode_approved = False
        orch.cancel_event = None
        orch._usage_buffer = {}
        orch._session_modified_files = set()

        resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
                            {"id": "tc2", "type": "function", "function": {"name": "edit_file_atomic", "arguments": '{"path":"a.py"}'}},
                        ],
                    }
                }
            ]
        }
        state = {
            "task": "Edit a.py",
            "working_dir": str(tmp_path),
            "history": [{"role": "user", "content": "Edit a.py"}],
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
                new=AsyncMock(return_value=resp),
            ),
        ):
            res = await frontier_loop_node(state, config={})

        assert res["awaiting_plan_approval"] is True
        assert res["plan_mode_blocked_tool"] == "edit_file_atomic"
        # No unresponded assistant(tool_calls=...) left dangling in history
        for m in res["history"]:
            assert not m.get("tool_calls")

    @pytest.mark.asyncio
    async def test_approval_node_requires_digest_and_does_not_auto_approve_preview(self):
        # Empty digest fails closed
        no_digest_pending = {"name": "write_file", "arguments": {"path": "a.py"}}
        res_missing = await _approval_node_impl(
            {"pending_action": no_digest_pending}, config=MagicMock()
        )
        assert res_missing["run"]["status"] == "cancelled"
        assert res_missing["plan_mode_approved"] is False

        # Earlier plan_mode_approved=True must NOT auto-approve a rejected preview
        pending = {
            "name": "write_file",
            "arguments": {"path": "a.py"},
            "digest": compute_action_digest("write_file", {"path": "a.py"}),
        }
        with patch(
            "src.core.orchestration.graph.nodes.approval_node._wait_for_user",
            new=AsyncMock(return_value={"preview_confirmed": False, "awaiting_user_input": False}),
        ):
            res_preview = await _approval_node_impl(
                {
                    "pending_action": pending,
                    "pending_preview_id": "prev-1",
                    "plan_mode_approved": True,
                },
                config=MagicMock(),
            )
        assert res_preview.get("plan_mode_approved") is not True
        assert res_preview.get("preview_confirmed") is False
        assert res_preview["run"]["outcome"] in ("rejected", "cancelled")


class TestTask08JsonCheckpointingAndSync:
    def test_jsonl_checkpoint_saver_uses_json_and_redacts_secrets(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("CODINGAGENT_GRAPH_CHECKPOINTING", "1")
        orch = SimpleNamespace(
            _working_dir=str(tmp_path),
            working_dir=str(tmp_path),
            _current_task_id="sess1",
        )
        saver = JsonlCheckpointSaver()
        cfg = {"configurable": {"thread_id": "sess1", "orchestrator": orch}}
        ckpt = {
            "v": 1,
            "id": "ckpt-1",
            "ts": "2026-11-16T00:00:00Z",
            "channel_values": {
                "task": "use key sk-secret1234567890abcdefghijklmnop",
                "count": 2,
            },
            "channel_versions": {"count": 1},
            "versions_seen": {},
        }
        # Verify pickle is completely removed from checkpoint_saver
        import src.core.orchestration.graph.checkpoint_saver as _cs_mod

        assert not hasattr(_cs_mod, "pickle")
        saver.put(cfg, ckpt, {"step": 1}, {"count": 1})
        tup = saver.get_tuple(cfg)

        assert tup is not None
        assert tup.checkpoint["channel_values"]["count"] == 2
        assert "sk-secret1234567890abcdefghijklmnop" not in tup.checkpoint["channel_values"]["task"]

    def test_rehydrate_ignores_cancelled_and_mismatched_tasks(self):
        fresh = {"task": "new task", "history": []}
        assert rehydrate_initial_state(fresh, {"task": "new task", "run": {"status": "cancelled"}, "errors": ["x"]}) == fresh
        assert rehydrate_initial_state(fresh, {"task": "old task", "run": {"status": "running"}, "errors": ["x"]}) == fresh

    def test_sync_msg_mgr_preserves_compaction_and_tool_call_fields(self):
        orch = SimpleNamespace(msg_mgr=MessageManager())
        orch.msg_mgr.append("user", "old 1")
        orch.msg_mgr.append("assistant", "old 2")
        orch.msg_mgr.append("user", "old 3")

        compacted_history = [
            {"role": "system", "content": "<summary>Conversation summary:\n- did work</summary>"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "ok"},
        ]
        _sync_msg_mgr_with_final_history(orch, {"history": compacted_history})
        msgs = orch.msg_mgr.all()
        assert len(msgs) == 3
        assert msgs[1]["tool_calls"][0]["id"] == "call_1"
        assert msgs[2]["tool_call_id"] == "call_1"
        assert msgs[2]["name"] == "read_file"

    def test_message_manager_truncates_tool_call_groups_atomically(self):
        mm = MessageManager(max_tokens=30)
        mm.append("system", "sys")
        mm.append(
            "assistant",
            "calling",
            tool_calls=[{"id": "t1", "function": {"name": "read_file", "arguments": "{}"}}],
        )
        mm.append("tool", "tool result payload " * 10, tool_call_id="t1", name="read_file")
        mm.append("user", "latest")
        remaining = mm.all()
        # No orphan role="tool" without preceding assistant(tool_calls)
        assert not any(
            m.get("role") == "tool"
            and (i == 0 or not remaining[i - 1].get("tool_calls"))
            for i, m in enumerate(remaining)
        )


class TestTask08ConfigReloadGateAndContract:
    def test_on_config_reloaded_reapplies_filter_and_updates_service(self):
        from src.core.orchestration.tool_registry import ToolRegistry

        reg = ToolRegistry()
        reg.register("read_file", lambda **kw: {})
        reg.register("bash", lambda **kw: {})

        orch = MagicMock()
        orch.working_dir = ""
        orch.mcp_manager = None
        orch._mcp_manager = None
        orch.tool_execution_service = MagicMock(_registry=None)

        ctx = ToolPermissionContext.from_iterables(deny_names=["bash"])
        with (
            patch("src.core.orchestration.registry_builder.example_registry", return_value=reg),
            patch("src.tools.permission_context._ACTIVE_CONTEXT", ctx),
        ):
            _on_config_reloaded(orch, set())

        assert "read_file" in orch.tool_registry
        assert "bash" not in orch.tool_registry
        assert orch.tool_registry.get_orchestrator() is orch
        assert orch.tool_execution_service._registry is orch.tool_registry

    @pytest.mark.asyncio
    async def test_approval_gate_wait_async_works_when_run_with_correlation_is_none(self):
        gate = AsyncGate(loop=None)
        gate._async_event = None
        gate.set()
        with patch("src.core.orchestration.approval_gate._run_with_correlation", None):
            assert await gate.wait_async(timeout=0.5) is True

    def test_completion_contract_handles_none_result_in_last_result(self):
        verdict = evaluate_completion_contract(
            task="Update foo.py",
            modified_files=[],
            last_result={"ok": False, "result": None},
            last_tool_name="edit_file_atomic",
            tool_calls_count=1,
        )
        assert verdict.is_complete is False
        assert verdict.task_type == TaskType.MUTATING
        assert verdict.failure_reason == "Mutation action failed"
