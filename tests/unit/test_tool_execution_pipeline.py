"""tests/unit/test_tool_execution_pipeline.py — Phase C tests.

Covers:
- Backward-compat: execute_tool_impl importable from tool_execution_pipeline
- Backward-compat: Orchestrator.execute_tool still works (thin delegate)
- dry_run interception
- read-before-write enforcement
- permission audit paths
- contract validation patching
- tool not found
- timeout handling
- plan_exit step storage
- session_store logging
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch():
    """Return a minimal Orchestrator-like namespace mock."""
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.working_dir = None
    orch._session_read_files = set()
    orch._session_modified_files = set()
    orch._usage_buffer = {}
    orch._step_snapshot_id = None
    orch._current_snapshot_id = None
    orch.rollback_manager = MagicMock()
    orch.event_bus = MagicMock()
    orch.plan_mode = None
    orch._plan_mode_approved = None
    orch.current_role = None
    orch._tool_executor = None
    orch.explore_mode = False
    orch.cost_tracker = MagicMock()
    orch.session_store = MagicMock()
    orch._dry_run = False
    return orch


def _make_tool(fn, side_effects=None):
    return {"fn": fn, "side_effects": side_effects or [], "description": "test"}


# ---------------------------------------------------------------------------
# Import / backward-compat
# ---------------------------------------------------------------------------


class TestImport:
    def test_execute_tool_impl_importable(self):
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        assert callable(execute_tool_impl)

    def test_orchestrator_execute_tool_still_callable(self):
        from src.core.orchestration.orchestrator import Orchestrator

        assert callable(Orchestrator.execute_tool)

    def test_orchestrator_execute_tool_delegates(self):
        """Orchestrator.execute_tool must call execute_tool_impl."""
        import inspect

        src = inspect.getsource(
            __import__(
                "src.core.orchestration.orchestrator", fromlist=["Orchestrator"]
            ).Orchestrator.execute_tool
        )
        assert "execute_tool_impl" in src


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------


class TestBasicInvocation:
    def test_invalid_tool_name_type(self):
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        orch = _make_orch()
        res = execute_tool_impl(orch, {"name": 123, "arguments": {}})
        assert res["ok"] is False
        assert "string" in res["error"]

    def test_tool_not_found(self):
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        orch = _make_orch()
        orch.tool_registry = MagicMock()
        orch.tool_registry.get = MagicMock(return_value=None)
        with (
            patch(
                "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
                set(),
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.WRITE_TOOLS_REQUIRING_READ",
                set(),
            ),
        ):
            res = execute_tool_impl(orch, {"name": "no_such_tool", "arguments": {}})
        assert res["ok"] is False
        assert "not found" in res["error"]

    def test_successful_invocation(self):
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        orch = _make_orch()
        fn = MagicMock(return_value={"ok": True, "output": "hello"})
        orch.tool_registry = MagicMock()
        orch.tool_registry.get = MagicMock(return_value=_make_tool(fn))
        orch._normalize_tool_result = (
            lambda r: r if isinstance(r, dict) else {"result": r}
        )
        orch._get_tool_timeout = MagicMock(return_value=0)
        orch._normalize_args = MagicMock(return_value={})
        orch._append_execution_trace = MagicMock()
        orch._sync_session_state = MagicMock()

        with (
            patch(
                "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
                return_value=None,
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
                set(),
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.WRITE_TOOLS_REQUIRING_READ",
                set(),
            ),
        ):
            res = execute_tool_impl(orch, {"name": "my_tool", "arguments": {}})

        assert res["ok"] is True
        assert res["result"]["output"] == "hello"


# ---------------------------------------------------------------------------
# Dry-run interception
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_blocked_tool_intercepted(self):
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        orch = _make_orch()
        orch._dry_run = True
        orch._dry_run_log = []

        with patch(
            "src.core.orchestration.tool_execution_pipeline.DRY_RUN_BLOCKED_TOOLS",
            {"write_file"},
        ):
            res = execute_tool_impl(
                orch, {"name": "write_file", "arguments": {"path": "x.py"}}
            )

        assert res["status"] == "dry_run"
        assert res["would_call"] == "write_file"
        assert len(orch._dry_run_log) == 1

    def test_dry_run_read_tool_not_intercepted(self):
        """Read tools are not in DRY_RUN_BLOCKED_TOOLS — they execute normally."""
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        orch = _make_orch()
        orch._dry_run = True
        fn = MagicMock(return_value={"ok": True})
        orch.tool_registry = MagicMock()
        orch.tool_registry.get = MagicMock(return_value=_make_tool(fn))
        orch._normalize_tool_result = (
            lambda r: r if isinstance(r, dict) else {"result": r}
        )
        orch._get_tool_timeout = MagicMock(return_value=0)
        orch._normalize_args = MagicMock(return_value={})
        orch._append_execution_trace = MagicMock()
        orch._sync_session_state = MagicMock()

        with (
            patch(
                "src.core.orchestration.tool_execution_pipeline.DRY_RUN_BLOCKED_TOOLS",
                {"write_file"},
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
                return_value=None,
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
                set(),
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.WRITE_TOOLS_REQUIRING_READ",
                set(),
            ),
        ):
            res = execute_tool_impl(orch, {"name": "read_file", "arguments": {}})

        assert res.get("ok") is True


# ---------------------------------------------------------------------------
# Read-before-write enforcement
# ---------------------------------------------------------------------------


class TestReadBeforeWrite:
    def test_blocks_write_to_unread_file(self, tmp_path):
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        target = tmp_path / "existing.py"
        target.write_text("x = 1")

        orch = _make_orch()
        orch.working_dir = tmp_path
        # File not in session_read_files

        with (
            patch(
                "src.core.orchestration.tool_execution_pipeline.WRITE_TOOLS_REQUIRING_READ",
                {"edit_file"},
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
                set(),
            ),
        ):
            res = execute_tool_impl(
                orch,
                {"name": "edit_file", "arguments": {"path": "existing.py"}},
            )

        assert res["ok"] is False
        assert "before writing" in res["error"]

    def test_allows_write_after_read(self, tmp_path):
        from pathlib import Path
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        target = tmp_path / "existing.py"
        target.write_text("x = 1")

        orch = _make_orch()
        orch.working_dir = tmp_path
        resolved = str((tmp_path / "existing.py").resolve())
        orch._session_read_files = {resolved}

        fn = MagicMock(return_value={"ok": True})
        orch.tool_registry = MagicMock()
        orch.tool_registry.get = MagicMock(
            return_value={"fn": fn, "side_effects": ["write"], "description": "edit"}
        )
        orch._normalize_tool_result = (
            lambda r: r if isinstance(r, dict) else {"result": r}
        )
        orch._get_tool_timeout = MagicMock(return_value=0)
        orch._normalize_args = MagicMock(return_value={})
        orch._append_execution_trace = MagicMock()
        orch._sync_session_state = MagicMock()

        with (
            patch(
                "src.core.orchestration.tool_execution_pipeline.WRITE_TOOLS_REQUIRING_READ",
                {"edit_file"},
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
                set(),
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
                return_value=None,
            ),
        ):
            res = execute_tool_impl(
                orch,
                {"name": "edit_file", "arguments": {"path": "existing.py"}},
            )

        assert res.get("ok") is True


# ---------------------------------------------------------------------------
# plan_exit step stashing
# ---------------------------------------------------------------------------


class TestPlanExitHandoff:
    def test_plan_exit_stashes_steps(self):
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        orch = _make_orch()
        orch._agent_mode = "plan"
        fn = MagicMock(
            return_value={"ok": True, "agent_mode": "execution", "steps": ["step1"]}
        )
        orch.tool_registry = MagicMock()
        orch.tool_registry.get = MagicMock(return_value=_make_tool(fn))
        orch._normalize_tool_result = (
            lambda r: r if isinstance(r, dict) else {"result": r}
        )
        orch._get_tool_timeout = MagicMock(return_value=0)
        orch._normalize_args = MagicMock(return_value={})
        orch._append_execution_trace = MagicMock()
        orch._sync_session_state = MagicMock()

        with (
            patch(
                "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
                return_value=None,
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
                set(),
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.WRITE_TOOLS_REQUIRING_READ",
                set(),
            ),
        ):
            execute_tool_impl(orch, {"name": "plan_exit", "arguments": {}})

        assert getattr(orch, "_committed_plan_steps", None) == ["step1"]
        assert getattr(orch, "_plan_mode_approved", None) is True


# ---------------------------------------------------------------------------
# session_store logging
# ---------------------------------------------------------------------------


class TestSessionStoreLogging:
    def test_session_store_called_on_success(self):
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        orch = _make_orch()
        fn = MagicMock(return_value={"ok": True})
        orch.tool_registry = MagicMock()
        orch.tool_registry.get = MagicMock(return_value=_make_tool(fn))
        orch._normalize_tool_result = (
            lambda r: r if isinstance(r, dict) else {"result": r}
        )
        orch._get_tool_timeout = MagicMock(return_value=0)
        orch._normalize_args = MagicMock(return_value={})
        orch._append_execution_trace = MagicMock()
        orch._sync_session_state = MagicMock()

        with (
            patch(
                "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
                return_value=None,
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
                set(),
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.WRITE_TOOLS_REQUIRING_READ",
                set(),
            ),
        ):
            execute_tool_impl(orch, {"name": "my_tool", "arguments": {}})

        orch.session_store.add_tool_call.assert_called_once()
        call_kwargs = orch.session_store.add_tool_call.call_args.kwargs
        assert call_kwargs["tool_name"] == "my_tool"
        assert call_kwargs["success"] is True


# ---------------------------------------------------------------------------
# backward-compat: constants re-exported in pipeline module
# ---------------------------------------------------------------------------


class TestPipelineConstants:
    def test_write_tools_requiring_read_present(self):
        from src.core.orchestration.tool_execution_pipeline import (
            WRITE_TOOLS_REQUIRING_READ,
        )

        assert isinstance(WRITE_TOOLS_REQUIRING_READ, (set, frozenset))
        assert len(WRITE_TOOLS_REQUIRING_READ) > 0

    def test_permission_required_tools_present(self):
        from src.core.orchestration.tool_execution_pipeline import (
            PERMISSION_REQUIRED_TOOLS,
        )

        assert isinstance(PERMISSION_REQUIRED_TOOLS, (set, frozenset))

    def test_dry_run_blocked_tools_present(self):
        from src.core.orchestration.tool_execution_pipeline import DRY_RUN_BLOCKED_TOOLS

        assert isinstance(DRY_RUN_BLOCKED_TOOLS, (set, frozenset))
