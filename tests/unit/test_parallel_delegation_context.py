"""Unit tests for delegate_tasks_parallel context isolation.

Regression coverage for Audit §4 High:
Parallel delegation re-entering one contextvars.Context concurrently causes
RuntimeError in concurrent workers. Each worker must have an independent copied context.
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from unittest.mock import patch

from src.tools.subagent_tools import delegate_tasks_parallel

_test_var: contextvars.ContextVar[str] = contextvars.ContextVar("_test_var", default="default")


def test_parallel_delegation_runs_concurrently(tmp_path: Path) -> None:
    _test_var.set("parent_context_val")

    subtasks = [
        {"role": "researcher", "subtask_description": "subtask 1"},
        {"role": "coder", "subtask_description": "subtask 2"},
        {"role": "reviewer", "subtask_description": "subtask 3"},
    ]

    def _fake_delegate(role, desc, workdir=None, allowed_tools=None, model=None):
        # Read context var to ensure context is intact and does not raise
        val = _test_var.get()
        return f"Completed {desc} with {val}"

    with patch("src.tools.subagent_tools.delegate_task", side_effect=_fake_delegate):
        result = delegate_tasks_parallel(subtasks=subtasks, working_dir=str(tmp_path))

    assert result.get("ok") is True
    assert result.get("succeeded") == 3
    assert result.get("failed") == 0
    sub_results = result.get("results", [])
    assert len(sub_results) == 3
    assert all(r.get("ok") is True for r in sub_results)
    assert all("Completed subtask" in r.get("output", "") for r in sub_results)


def test_batch_parallel_execution_and_context_propagation(tmp_path: Path) -> None:
    from src.core.orchestration.orchestrator import Orchestrator

    for i in range(1, 5):
        (tmp_path / f"f{i}.txt").write_text(f"content {i}")

    orch = Orchestrator(working_dir=str(tmp_path))
    res = orch.execute_tool({
        "name": "batch",
        "arguments": {
            "calls": [
                {"tool": "read_file", "input": {"path": f"f{i}.txt"}}
                for i in range(1, 5)
            ]
        },
    })
    assert res.get("ok") is True
    batch_res = res.get("result", {})
    assert batch_res.get("status") == "ok"
    assert len(batch_res.get("results", [])) == 4
    for i, item in enumerate(batch_res["results"], start=1):
        assert item["status"] == "ok"
        assert item["result"]["ok"] is True
        assert item["result"]["result"]["content"] == f"content {i}"


def test_delegate_tasks_parallel_active_agent_barrier_isolation(tmp_path: Path) -> None:
    import threading
    from src.core.orchestration.orchestrator import Orchestrator

    barrier = threading.Barrier(2)
    results_by_role = {}

    def _simulated_delegate(role, desc, workdir=None, allowed_tools=None, model=None):
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR
        parent_orch = _PARENT_ORCHESTRATOR_VAR.get(None)
        from src.core.orchestration.agent_types import AgentDefinition
        from src.core.orchestration.tool_execution_pipeline import _ACTIVE_AGENT_CTX

        agent_def = AgentDefinition(
            id=f"delegated_{role}",
            name=f"Delegated {role}",
            description=f"Delegated {role} subagent",
            allowed_tools=allowed_tools,
        )
        if parent_orch is not None:
            parent_orch.active_agent = agent_def
        token = _ACTIVE_AGENT_CTX.set(agent_def)
        try:
            # Synchronize both threads at the barrier so both are in their active state simultaneously
            barrier.wait(timeout=5)
            # Both threads attempt to call write_file through parent_orch
            # Before write_file, do a read to satisfy read_before_write if needed
            (Path(workdir or str(tmp_path)) / f"{role}.txt").write_text("initial")
            parent_orch.execute_tool({
                "name": "read_file",
                "arguments": {"path": f"{role}.txt"},
            })
            call_res = parent_orch.execute_tool({
                "name": "write_file",
                "arguments": {"path": f"{role}.txt", "content": "updated data"},
            })
            results_by_role[role] = call_res
            return f"Result: {call_res.get('ok')}"
        finally:
            _ACTIVE_AGENT_CTX.reset(token)
            if parent_orch and getattr(parent_orch, "active_agent", None) is agent_def:
                parent_orch.active_agent = None

    orch = Orchestrator(working_dir=str(tmp_path))
    from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR
    token = _PARENT_ORCHESTRATOR_VAR.set(orch)
    try:
        subtasks = [
            {"role": "reviewer", "subtask_description": "task 1", "allowed_tools": ["read_file"]},
            {"role": "operational", "subtask_description": "task 2", "allowed_tools": ["read_file", "write_file"]},
        ]
        with patch("src.tools.subagent_tools.delegate_task", side_effect=_simulated_delegate):
            res = delegate_tasks_parallel(subtasks=subtasks, working_dir=str(tmp_path))
        assert res.get("ok") is True
        # Reviewer must have been blocked from calling write_file
        reviewer_call = results_by_role.get("reviewer")
        assert reviewer_call is not None
        assert reviewer_call.get("ok") is False
        assert "not permitted" in reviewer_call.get("error", "").lower()
        # Operational must have succeeded in calling write_file
        op_call = results_by_role.get("operational")
        assert op_call is not None
        assert op_call.get("ok") is True
    finally:
        _PARENT_ORCHESTRATOR_VAR.reset(token)
