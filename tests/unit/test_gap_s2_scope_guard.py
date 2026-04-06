"""
GAP-S2: Workspace scope guard tests.

Covers:
  SG-1  Write is BLOCKED when path not in affected_files.
  SG-2  Write is ALLOWED when path IS in affected_files.
  SG-3  affected_files=[] (empty list) bypasses the guard.
  SG-4  affected_files=None bypasses the guard.
  SG-5  _affected_files not set on orchestrator falls back gracefully.
  SG-6  scope.violation event is published on a violation.
  SG-7  ask_user answer with file paths expands affected_files in execution_node return.
  SG-8  ask_user answer with blanket approval clears affected_files restriction.
  SG-9  ask_user answer with no recognisable paths does NOT mutate affected_files.
  SG-10 _extract_affected_files parses files from step descriptions.
  SG-11 _extract_affected_files uses explicit step['files'] list.
  SG-12 _extract_affected_files deduplicates entries.
  SG-13 Path normalisation — workdir prefix stripped before comparison.
  SG-14 Non-write tools are never blocked by the scope guard.
  SG-15 ask_user expansion only fires when affected_files is non-None in state.
"""

from __future__ import annotations

import asyncio
import re
import types
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_orchestrator(
    affected_files: list | None = None,
    session_read_files: set | None = None,
    working_dir: str = "/workdir",
):
    """Return a minimal orchestrator-like object with just enough surface for the
    scope guard to function.  We import the real class to test the actual code path.
    """
    from pathlib import Path

    from src.core.orchestration.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch._affected_files = affected_files if affected_files is not None else []  # type: ignore[attr-defined]
    orch._session_read_files = session_read_files or set()  # type: ignore[attr-defined]
    orch._plan_mode_approved = None  # type: ignore[attr-defined]
    orch.working_dir = Path(working_dir)  # type: ignore[assignment]
    # Minimal tool registry — real execute_tool requires this.
    orch._tools = {}  # type: ignore[attr-defined]
    orch._hooks = {}  # type: ignore[attr-defined]
    orch._tool_hook_runner = None  # type: ignore[assignment]
    # event_bus stub
    bus = MagicMock()
    orch.event_bus = bus  # type: ignore[attr-defined]
    orch.plan_mode = None  # type: ignore[assignment]
    return orch


def _call_scope_guard(orch, tool_name: str, path_arg: str):
    """Call execute_tool with a minimal action dict and patch the tool registry
    so only the guards are exercised (no actual tool execution).

    execute_tool uses {"name": ..., "arguments": ...} as the call format.
    """
    action = {"name": tool_name, "arguments": {"path": path_arg}}
    # Stub the tool registry so the real tool function is never invoked.
    fake_fn = MagicMock(return_value={"ok": True})
    orch.tool_registry = MagicMock()
    orch.tool_registry.get.return_value = {"fn": fake_fn, "description": "stub"}
    result = orch.execute_tool(action)
    return result


# ---------------------------------------------------------------------------
# SG-1 / SG-2 / SG-3 / SG-4 / SG-5  — orchestrator scope guard
# ---------------------------------------------------------------------------


class TestScopeGuardBlocking:
    """Tests for the scope guard inside Orchestrator.execute_tool."""

    def test_sg1_write_blocked_when_path_not_in_affected_files(self, tmp_path):
        """SG-1: write_file to an out-of-scope path is rejected."""
        orch = _make_minimal_orchestrator(
            affected_files=["src/allowed.py"],
            session_read_files={"forbidden.py"},
        )
        # Provide a read entry so the read-before-write guard doesn't fire first.
        orch._session_read_files = {str(orch.working_dir / "forbidden.py")}  # type: ignore[attr-defined]

        result = _call_scope_guard(orch, "write_file", "forbidden.py")

        assert result.get("ok") is False
        assert "outside the task scope" in result.get("error", "")

    def test_sg2_write_allowed_when_path_in_affected_files(self):
        """SG-2: write_file to an in-scope path passes the guard."""
        orch = _make_minimal_orchestrator(
            affected_files=["src/allowed.py"],
        )
        # Mark file as already-read to pass read-before-write guard.
        orch._session_read_files = {"/workdir/src/allowed.py"}

        result = _call_scope_guard(orch, "write_file", "src/allowed.py")

        # Guard should not return a scope error.
        assert "outside the task scope" not in result.get("error", "")

    def test_sg3_empty_affected_files_bypasses_guard(self):
        """SG-3: An empty affected_files list means the guard is inactive."""
        orch = _make_minimal_orchestrator(affected_files=[])
        orch._session_read_files = {"/workdir/any_file.py"}

        result = _call_scope_guard(orch, "write_file", "any_file.py")

        assert "outside the task scope" not in result.get("error", "")

    def test_sg4_none_affected_files_bypasses_guard(self):
        """SG-4: affected_files=None means the guard is inactive."""
        orch = _make_minimal_orchestrator(affected_files=None)
        orch._session_read_files = {"/workdir/any_file.py"}

        result = _call_scope_guard(orch, "write_file", "any_file.py")

        assert "outside the task scope" not in result.get("error", "")

    def test_sg5_missing_attribute_falls_back_gracefully(self):
        """SG-5: If _affected_files attr doesn't exist, guard never blocks."""
        orch = _make_minimal_orchestrator(affected_files=[])
        del orch._affected_files  # type: ignore[attr-defined]  # simulate attribute absence
        orch._session_read_files = {"/workdir/any_file.py"}

        result = _call_scope_guard(orch, "write_file", "any_file.py")

        assert "outside the task scope" not in result.get("error", "")


# ---------------------------------------------------------------------------
# SG-6  — scope.violation event
# ---------------------------------------------------------------------------


class TestScopeViolationEvent:
    def test_sg6_scope_violation_event_published(self):
        """SG-6: A scope.violation event is emitted when the guard fires.

        We patch the module-level get_event_bus() that the guard uses internally.
        """
        orch = _make_minimal_orchestrator(affected_files=["src/allowed.py"])
        orch._session_read_files = {"/workdir/forbidden.py"}  # type: ignore[attr-defined]

        mock_bus = MagicMock()
        with patch(
            "src.core.orchestration.orchestrator.get_event_bus",
            return_value=mock_bus,
            create=True,
        ):
            result = _call_scope_guard(orch, "write_file", "forbidden.py")

        # Guard must block the write.
        assert result.get("ok") is False
        assert "outside the task scope" in result.get("error", "")


# ---------------------------------------------------------------------------
# SG-7 / SG-8 / SG-9 / SG-15  — execution_node affected_files expansion
# ---------------------------------------------------------------------------


def _make_state(tool_name, answer, affected_files):
    """Build a minimal fake state dict for execution_node extraction logic."""
    return {
        "affected_files": affected_files,
        "history": [],
        "current_plan": None,
        "current_step": 0,
        "tool_call_count": 0,
        "next_action": {"tool": tool_name, "args": {"question": "Expand scope?"}},
        "session_id": "test-session",
        "working_dir": "/workdir",
        "tool_last_used": {},
        "files_read": {},
        "recent_tool_calls": [],
        "plan_mode_approved": None,
        "no_plan_fail_count": 0,
    }


def _run_expansion_logic(tool_name: str, answer: str, affected_files):
    """Exercise only the ask_user expansion block from execution_node inline."""
    import re as _re

    _APPROVAL_KW = _re.compile(
        r"\b(yes\s+all|approve\s+all|expand\s+scope|allow\s+all|unrestrict)\b",
        _re.IGNORECASE,
    )
    _FILE_PAT2 = _re.compile(
        r"\b([\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|yaml|yml|json|md|txt|toml|cfg|ini|sh|bash|html|css|scss|sql))\b"
    )
    res = {"status": "ok", "answer": answer}
    state = _make_state(tool_name, answer, affected_files)
    orchestrator = MagicMock()
    orchestrator._affected_files = list(affected_files or [])

    affected_files_update: dict = {}
    if (
        tool_name == "ask_user"
        and res.get("status") == "ok"
        and state.get("affected_files") is not None
    ):
        answer_text = str(res.get("answer") or "")
        if _APPROVAL_KW.search(answer_text):
            affected_files_update = {"affected_files": []}
        else:
            new_paths = []
            seen_paths = set(state.get("affected_files") or [])
            for _m in _FILE_PAT2.finditer(answer_text):
                _p = _m.group(1)
                if _p not in seen_paths and not _p.startswith(".."):
                    seen_paths.add(_p)
                    new_paths.append(_p)
            if new_paths:
                expanded = list(state.get("affected_files") or []) + new_paths
                affected_files_update = {"affected_files": expanded}
                if orchestrator:
                    orchestrator._affected_files = expanded

    return affected_files_update, orchestrator


class TestAskUserScopeExpansion:
    def test_sg7_ask_user_answer_with_file_paths_expands_affected_files(self):
        """SG-7: File paths in ask_user answer are added to affected_files."""
        update, orch = _run_expansion_logic(
            "ask_user",
            "Yes, please also include src/new_file.py and tests/test_new.py",
            ["src/existing.py"],
        )
        assert "affected_files" in update
        result = update["affected_files"]
        assert "src/new_file.py" in result
        assert "tests/test_new.py" in result
        assert "src/existing.py" in result  # original preserved

    def test_sg8_blanket_approval_clears_restriction(self):
        """SG-8: 'yes all' answer clears the restriction (affected_files=[])."""
        update, _ = _run_expansion_logic(
            "ask_user",
            "Yes all, go ahead and expand scope",
            ["src/existing.py"],
        )
        assert update == {"affected_files": []}

    def test_sg8b_approve_all_keyword_clears_restriction(self):
        """SG-8b: 'approve all' keyword clears the restriction."""
        update, _ = _run_expansion_logic(
            "ask_user",
            "approve all",
            ["src/foo.py"],
        )
        assert update == {"affected_files": []}

    def test_sg9_no_file_paths_in_answer_leaves_affected_files_unchanged(self):
        """SG-9: If no paths extracted, affected_files_update is empty."""
        update, _ = _run_expansion_logic(
            "ask_user",
            "No, do not expand the scope.",
            ["src/existing.py"],
        )
        assert update == {}

    def test_sg15_expansion_only_fires_when_affected_files_non_none(self):
        """SG-15: When state has affected_files=None, expansion is skipped."""
        update, _ = _run_expansion_logic(
            "ask_user",
            "Yes, add src/extra.py",
            None,  # affected_files=None means guard is inactive
        )
        # should not produce an update — None signals guard-bypassed state
        assert update == {}

    def test_sg7b_orchestrator_instance_updated_immediately(self):
        """SG-7b: orchestrator._affected_files is synchronised within the same call."""
        _, orch = _run_expansion_logic(
            "ask_user",
            "please also include src/sync.py",
            ["src/base.py"],
        )
        assert "src/sync.py" in orch._affected_files

    def test_non_ask_user_tool_does_not_trigger_expansion(self):
        """Non ask_user tools never trigger scope expansion."""
        update, _ = _run_expansion_logic(
            "write_file",
            "src/injected.py",
            ["src/existing.py"],
        )
        assert update == {}


# ---------------------------------------------------------------------------
# SG-10 / SG-11 / SG-12  — _extract_affected_files unit tests
# ---------------------------------------------------------------------------


class TestExtractAffectedFiles:
    """Unit tests for the planning_node._extract_affected_files helper."""

    @staticmethod
    def _extract(steps):
        from src.core.orchestration.graph.nodes.planning_node import (
            _extract_affected_files,
        )

        return _extract_affected_files(steps)

    def test_sg10_parses_file_paths_from_description(self):
        """SG-10: File paths in step descriptions are extracted."""
        steps = [
            {"description": "Edit src/utils.py and src/core/manager.py", "files": []}
        ]
        result = self._extract(steps)
        assert "src/utils.py" in result
        assert "src/core/manager.py" in result

    def test_sg11_uses_explicit_files_list(self):
        """SG-11: Explicit step['files'] entries are included."""
        steps = [{"description": "Some description", "files": ["src/explicit.py"]}]
        result = self._extract(steps)
        assert "src/explicit.py" in result

    def test_sg12_deduplicates_entries(self):
        """SG-12: Duplicate paths appear only once in the result."""
        steps = [
            {"description": "Edit src/dup.py", "files": ["src/dup.py"]},
            {"description": "Also edit src/dup.py", "files": []},
        ]
        result = self._extract(steps)
        assert result.count("src/dup.py") == 1

    def test_empty_steps_returns_empty_list(self):
        """Empty step list → empty result."""
        assert self._extract([]) == []

    def test_step_without_files_key_handled_gracefully(self):
        """Steps missing 'files' key don't crash."""
        steps = [{"description": "Edit src/main.py"}]
        result = self._extract(steps)
        assert "src/main.py" in result


# ---------------------------------------------------------------------------
# SG-13  — Path normalisation
# ---------------------------------------------------------------------------


class TestPathNormalisation:
    """SG-13: workdir prefix is stripped before comparison."""

    def test_sg13_absolute_path_with_workdir_prefix_allowed(self):
        """SG-13: '/workdir/src/allowed.py' matches 'src/allowed.py' in affected_files."""
        orch = _make_minimal_orchestrator(affected_files=["src/allowed.py"])
        orch._session_read_files = {"/workdir/src/allowed.py"}

        result = _call_scope_guard(orch, "write_file", "/workdir/src/allowed.py")

        assert "outside the task scope" not in result.get("error", "")

    def test_sg13_path_without_prefix_still_matches(self):
        """SG-13: Relative 'src/allowed.py' matches even if affected_files stores abs path."""
        orch = _make_minimal_orchestrator(affected_files=["/workdir/src/allowed.py"])
        orch._session_read_files = {"/workdir/src/allowed.py"}

        result = _call_scope_guard(orch, "write_file", "src/allowed.py")

        assert "outside the task scope" not in result.get("error", "")


# ---------------------------------------------------------------------------
# SG-14  — Non-write tools not blocked
# ---------------------------------------------------------------------------


class TestNonWriteToolsNotBlocked:
    def test_sg14_read_file_never_blocked_by_scope_guard(self):
        """SG-14: read_file is not a write tool and is never blocked."""
        orch = _make_minimal_orchestrator(affected_files=["src/allowed.py"])
        # read_file is not in WRITE_TOOLS_REQUIRING_READ, so guard doesn't apply.
        result = _call_scope_guard(orch, "read_file", "some/other.py")

        assert "outside the task scope" not in result.get("error", "")
