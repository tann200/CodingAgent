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
