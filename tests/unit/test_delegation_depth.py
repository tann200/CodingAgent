"""Tests for delegation depth propagation and enforcement.

These tests verify that delegate_task uses the ContextVar for in-process
depth tracking, that initial_state['delegation_depth'] is set to parent+1,
and that the delegation_node enforces the state-based depth limit when using
the PRSW (file-locking) path. A concurrency test ensures ContextVar values
from different callers do not leak between each other.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_minimal_graph_mock(captured_list: list):
    async def _fake_ainvoke(state, config=None):
        # Record the delegation_depth observed inside the child run
        captured_list.append(state.get("delegation_depth"))
        # Small sleep to increase the chance of overlap in concurrency test
        await asyncio.sleep(0.02)
        return {
            "task": state.get("task", ""),
            "history": [{"role": "assistant", "content": "Done."}],
            "errors": [],
            "last_result": {"status": "ok"},
            "session_id": state.get("session_id", "test"),
        }

    mock_graph = MagicMock()
    mock_graph.ainvoke = _fake_ainvoke
    return mock_graph


def test_delegate_task_sets_initial_state_delegation_depth(tmp_path: Path) -> None:
    captured: dict = {}

    async def _fake_ainvoke(state, config=None):
        # capture the full state dict for inspection
        captured.update(state)
        return {
            "task": state.get("task", ""),
            "history": [{"role": "assistant", "content": "Done."}],
            "errors": [],
            "last_result": {"status": "ok"},
            "session_id": state.get("session_id", "test"),
        }

    mock_graph = MagicMock()
    mock_graph.ainvoke = _fake_ainvoke

    with (
        patch("src.tools.subagent_tools._resolve_subagent_graph") as mock_gf,
        patch("src.tools.subagent_tools._get_agent_brain_manager") as mock_brain_mgr,
        patch("src.tools.subagent_tools._PARENT_ORCHESTRATOR_VAR") as mock_ctxvar,
    ):
        mock_gf.return_value = mock_graph
        mock_brain = MagicMock()
        mock_brain.compile_system_prompt.return_value = "sys"
        mock_brain_mgr.return_value = mock_brain
        mock_ctxvar.get.return_value = None

        from src.tools.subagent_tools import delegate_task, _DELEGATION_DEPTH_VAR

        token = _DELEGATION_DEPTH_VAR.set(1)
        try:
            delegate_task(
                role="analyst",
                subtask_description="verify depth",
                working_dir=str(tmp_path),
            )
        finally:
            _DELEGATION_DEPTH_VAR.reset(token)

    # initial_state['delegation_depth'] should be parent_depth + 1 == 2
    assert captured.get("delegation_depth") == 2


def test_delegation_node_refuses_when_state_depth_at_max(tmp_path: Path) -> None:
    from src.core.orchestration.graph.nodes.delegation_node import delegation_node

    # Build a PRSW-style state (has files + fake lock manager) so the
    # _execute_delegation_with_locks path is exercised and the state-based
    # depth guard runs.
    state = {
        "delegations": [
            {
                "role": "researcher",
                "task": "do work",
                "result_key": "rk",
                "files": ["f1"],
            }
        ],
        "delegation_depth": 3,
        "working_dir": str(tmp_path),
        "session_id": "s1",
    }

    class _SimpleLockManager:
        async def acquire_read_async(self, f, agent_id):
            return True

        async def release_read(self, f, agent_id):
            return None

    state["_file_lock_manager"] = _SimpleLockManager()

    result = asyncio.run(delegation_node(state, config=None))

    # The PRSW path should return an error for the delegation due to depth limit
    dr = result.get("delegation_results", {}).get("rk")
    assert dr is not None
    assert dr.get("status") == "error"
    assert "delegation depth limit" in dr.get("error", "")


def test_delegate_task_concurrent_contextvar_isolation(tmp_path: Path) -> None:
    captured: list = []
    mock_graph = _make_minimal_graph_mock(captured)

    with (
        patch("src.tools.subagent_tools._resolve_subagent_graph") as mock_gf,
        patch("src.tools.subagent_tools._get_agent_brain_manager") as mock_brain_mgr,
        patch("src.tools.subagent_tools._PARENT_ORCHESTRATOR_VAR") as mock_ctxvar,
    ):
        mock_gf.return_value = mock_graph
        mock_brain = MagicMock()
        mock_brain.compile_system_prompt.return_value = "sys"
        mock_brain_mgr.return_value = mock_brain
        mock_ctxvar.get.return_value = None

        from src.tools.subagent_tools import delegate_task, _DELEGATION_DEPTH_VAR

        def _call_delegate(parent_depth: int) -> None:
            token = _DELEGATION_DEPTH_VAR.set(parent_depth)
            try:
                delegate_task(
                    role="analyst",
                    subtask_description=f"concurrent {parent_depth}",
                    working_dir=str(tmp_path),
                )
            finally:
                _DELEGATION_DEPTH_VAR.reset(token)

        # Run two delegate_task calls in parallel with different parent depths.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(_call_delegate, 0), ex.submit(_call_delegate, 2)]
            for f in futs:
                f.result()

    # Ensure both child runs observed the correct child depth values (1 and 3)
    assert set(captured) == {1, 3}
