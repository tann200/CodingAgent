"""Tests for delegate_tasks_parallel (P2-8 — async subagent delegation)."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_subtask(role: str = "analyst", desc: str = "do something") -> dict:
    return {"role": role, "subtask_description": desc}


def _patch_delegate(return_value: str = "done"):
    """Patch delegate_task to return a fixed value instantly."""
    return patch(
        "src.tools.subagent_tools.delegate_task",
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

class TestImport:
    def test_tool_importable(self):
        from src.tools.subagent_tools import delegate_tasks_parallel
        assert callable(delegate_tasks_parallel)

    def test_tool_is_decorated(self):
        from src.tools.subagent_tools import delegate_tasks_parallel
        from src.tools._tool import TOOL_ATTR
        assert hasattr(delegate_tasks_parallel, TOOL_ATTR)

    def test_tool_in_denylist(self):
        from src.tools.subagent_payloads import DELEGATION_DENYLIST
        assert "delegate_tasks_parallel" in DELEGATION_DENYLIST


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def setup_method(self):
        from src.tools.subagent_tools import delegate_tasks_parallel
        self.fn = delegate_tasks_parallel

    def test_empty_list(self):
        result = self.fn(subtasks=[])
        assert result["ok"] is False
        assert "non-empty" in result["error"]

    def test_non_list(self):
        result = self.fn(subtasks="not a list")  # type: ignore
        assert result["ok"] is False

    def test_too_many_subtasks(self):
        from src.tools.subagent_tools import _PARALLEL_MAX_SUBTASKS
        tasks = [_make_subtask(desc=f"task {i}") for i in range(_PARALLEL_MAX_SUBTASKS + 1)]
        result = self.fn(subtasks=tasks)
        assert result["ok"] is False
        assert "Too many subtasks" in result["error"]

    def test_subtask_not_dict(self):
        result = self.fn(subtasks=["not a dict"])
        assert result["ok"] is False
        assert "dict" in result["error"]

    def test_missing_role(self):
        result = self.fn(subtasks=[{"subtask_description": "do stuff"}])
        assert result["ok"] is False
        assert "role" in result["error"]

    def test_missing_description(self):
        result = self.fn(subtasks=[{"role": "analyst"}])
        assert result["ok"] is False
        assert "subtask_description" in result["error"]

    def test_empty_role(self):
        result = self.fn(subtasks=[{"role": "", "subtask_description": "do stuff"}])
        assert result["ok"] is False

    def test_empty_description(self):
        result = self.fn(subtasks=[{"role": "analyst", "subtask_description": ""}])
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Successful parallel delegation
# ---------------------------------------------------------------------------

class TestSuccessfulDelegation:
    def setup_method(self):
        from src.tools.subagent_tools import delegate_tasks_parallel
        self.fn = delegate_tasks_parallel

    def test_single_subtask_success(self):
        with _patch_delegate("analysis complete"):
            result = self.fn(subtasks=[_make_subtask()])
        assert result["ok"] is True
        assert result["succeeded"] == 1
        assert result["failed"] == 0
        assert len(result["results"]) == 1

    def test_multiple_subtasks_all_succeed(self):
        with _patch_delegate("done"):
            result = self.fn(subtasks=[_make_subtask(desc=f"task {i}") for i in range(3)])
        assert result["ok"] is True
        assert result["succeeded"] == 3
        assert result["failed"] == 0
        assert len(result["results"]) == 3

    def test_result_contains_role_and_description(self):
        with _patch_delegate("output text"):
            result = self.fn(subtasks=[{"role": "coding", "subtask_description": "fix bug"}])
        r = result["results"][0]
        assert r["role"] == "coding"
        assert r["subtask_description"] == "fix bug"
        assert r["output"] == "output text"
        assert r["ok"] is True

    def test_working_dir_propagated(self):
        calls = []

        def fake_delegate(role, desc, working_dir=None, allowed_tools=None, model=None):
            calls.append({"working_dir": working_dir})
            return "done"

        with patch("src.tools.subagent_tools.delegate_task", side_effect=fake_delegate):
            self.fn(subtasks=[_make_subtask()], working_dir="/tmp/proj")

        assert calls[0]["working_dir"] == "/tmp/proj"

    def test_per_subtask_working_dir_overrides_default(self):
        calls = []

        def fake_delegate(role, desc, working_dir=None, allowed_tools=None, model=None):
            calls.append({"working_dir": working_dir})
            return "done"

        with patch("src.tools.subagent_tools.delegate_task", side_effect=fake_delegate):
            self.fn(
                subtasks=[{"role": "analyst", "subtask_description": "x", "working_dir": "/override"}],
                working_dir="/default",
            )

        assert calls[0]["working_dir"] == "/override"

    def test_model_propagated(self):
        calls = []

        def fake_delegate(role, desc, working_dir=None, allowed_tools=None, model=None):
            calls.append({"model": model})
            return "done"

        with patch("src.tools.subagent_tools.delegate_task", side_effect=fake_delegate):
            self.fn(subtasks=[_make_subtask()], model="gpt-4o")

        assert calls[0]["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# Partial failure handling
# ---------------------------------------------------------------------------

class TestPartialFailure:
    def setup_method(self):
        from src.tools.subagent_tools import delegate_tasks_parallel
        self.fn = delegate_tasks_parallel

    def test_one_fails_ok_is_false(self):
        results_seq = ["done", "Error: something went wrong"]

        def fake_delegate(role, desc, working_dir=None, allowed_tools=None, model=None):
            return results_seq.pop(0)

        with patch("src.tools.subagent_tools.delegate_task", side_effect=fake_delegate):
            result = self.fn(subtasks=[_make_subtask(desc=f"t{i}") for i in range(2)])

        assert result["ok"] is False
        assert result["succeeded"] == 1
        assert result["failed"] == 1

    def test_exception_in_subtask_captured(self):
        def boom(role, desc, working_dir=None, allowed_tools=None, model=None):
            raise RuntimeError("network failure")

        with patch("src.tools.subagent_tools.delegate_task", side_effect=boom):
            result = self.fn(subtasks=[_make_subtask()])

        assert result["ok"] is False
        assert result["failed"] == 1
        r = result["results"][0]
        assert "RuntimeError" in r["error"] or "network failure" in r.get("error", "")

    def test_all_fail_returns_zero_succeeded(self):
        with _patch_delegate("Error: provider offline"):
            result = self.fn(subtasks=[_make_subtask(desc=f"t{i}") for i in range(2)])
        assert result["succeeded"] == 0
        assert result["failed"] == 2


# ---------------------------------------------------------------------------
# Parallelism verification
# ---------------------------------------------------------------------------

class TestParallelism:
    def test_all_subtasks_are_executed(self):
        """Verify every submitted subtask is actually run."""
        executed = []

        def fake_delegate(role, desc, working_dir=None, allowed_tools=None, model=None):
            executed.append(desc)
            return "done"

        from src.tools.subagent_tools import delegate_tasks_parallel
        tasks = [_make_subtask(desc=f"task_{i}") for i in range(4)]
        with patch("src.tools.subagent_tools.delegate_task", side_effect=fake_delegate):
            result = delegate_tasks_parallel(subtasks=tasks)

        assert result["succeeded"] == 4
        assert len(executed) == 4
        for i in range(4):
            assert f"task_{i}" in executed
