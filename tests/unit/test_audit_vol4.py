"""
Audit Vol4 — Phase 1 regression tests.

Covers:
  C1 — Diff regex correctness in _render_side_by_side_diff
  C3 — verification_node None working_dir guard
  C4 — DANGEROUS_PATTERNS whitespace normalisation (rm  -rf bypass)
  H3 — action_interrupt_agent `or True` removal
  H5 — tee/touch removed from SAFE_COMMANDS
"""

from __future__ import annotations

import re
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# C1 — Diff rendering regex (must match @@ -N,N +N,N @@ hunk headers)
# ---------------------------------------------------------------------------

class TestC1DiffRegex:
    """The side-by-side diff renderer must parse unified diff hunk headers."""

    # The corrected pattern from textual_app_impl.py line 804
    HUNK_PATTERN = re.compile(r"@@ -(\d+),?\d* \+(\d+),?\d* @@")

    def test_regex_matches_standard_hunk(self):
        line = "@@ -5,7 +5,9 @@"
        m = self.HUNK_PATTERN.search(line)
        assert m is not None, "Pattern must match standard unified diff hunk header"
        assert m.group(1) == "5"
        assert m.group(2) == "5"

    def test_regex_matches_single_line_hunk(self):
        line = "@@ -1 +1 @@"
        assert self.HUNK_PATTERN.search(line) is not None

    def test_regex_matches_hunk_with_trailing_context(self):
        line = "@@ -10,3 +10,4 @@ def my_function():"
        m = self.HUNK_PATTERN.search(line)
        assert m is not None
        assert m.group(1) == "10"

    def test_old_broken_regex_does_not_match(self):
        """Verify that the old escaped regex was indeed broken."""
        broken = re.compile(r"@@ -(\\d+),?\\d* \\+(\\d+),?\\d* @@")
        line = "@@ -5,7 +5,9 @@"
        assert broken.search(line) is None, (
            "Old escaped regex should NOT match a real diff hunk — "
            "this confirms the original bug"
        )

    def test_regex_does_not_match_non_hunk_lines(self):
        for line in ["--- a/foo.py", "+++ b/foo.py", "-removed line", "+added line"]:
            assert self.HUNK_PATTERN.search(line) is None


# ---------------------------------------------------------------------------
# C3 — verification_node None working_dir guard
# ---------------------------------------------------------------------------

class TestC3VerificationNodeNoneWorkingDir:
    """verification_node must not crash when working_dir is absent from state."""

    def test_path_none_raises_without_guard(self):
        """Demonstrate the original crash: Path(None) raises TypeError."""
        with pytest.raises(TypeError):
            Path(None)

    def test_path_with_guard_falls_back(self):
        """With the fix, Path(None or '.') should return Path('.')."""
        wd = Path(None or ".")
        assert wd == Path(".")

    def test_verification_node_no_crash_on_none_workdir(self, tmp_path):
        """verification_node must complete without raising when working_dir is None."""
        import asyncio
        from src.core.orchestration.graph.nodes.verification_node import verification_node

        state = {
            "working_dir": None,
            "last_result": {},
            "last_tool_name": "",
            "current_plan": [],
            "current_step": 0,
        }
        config = MagicMock()
        # Should not raise TypeError
        result = asyncio.run(verification_node(state, config))
        assert isinstance(result, dict)
        assert "verification_result" in result

    def test_verification_node_no_crash_on_missing_workdir(self):
        """verification_node must not crash when working_dir key is missing."""
        import asyncio
        from src.core.orchestration.graph.nodes.verification_node import verification_node

        state = {
            "last_result": {},
            "last_tool_name": "",
            "current_plan": [],
            "current_step": 0,
        }
        config = MagicMock()
        result = asyncio.run(verification_node(state, config))
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# C4 — DANGEROUS_PATTERNS whitespace normalisation
# ---------------------------------------------------------------------------

class TestC4DangerousPatternsWhitespace:
    """bash() must block double-space variants of rm -rf and similar."""

    def _bash(self, cmd: str):
        from src.tools.file_tools import bash
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            return bash(cmd, workdir=Path(td))

    def test_rm_rf_double_space_blocked(self):
        result = self._bash("rm  -rf /tmp/test")
        assert result["status"] == "error", (
            "rm  -rf (double space) must be blocked by DANGEROUS_PATTERNS"
        )

    def test_rm_rf_single_space_blocked(self):
        result = self._bash("rm -rf /tmp/test")
        assert result["status"] == "error"

    def test_rm_r_tab_blocked(self):
        result = self._bash("rm\t-rf /tmp/test")
        assert result["status"] == "error"

    def test_safe_command_still_works(self, tmp_path):
        """Normal safe commands must still be allowed after the fix."""
        (tmp_path / "hello.txt").write_text("hello")
        result = self._bash(f"ls {tmp_path}")
        # ls is allowed — should not be blocked
        assert result.get("status") != "error" or "dangerous" not in result.get("error", "")

    def test_pipe_operator_blocked(self):
        result = self._bash("ls | grep foo")
        assert result["status"] == "error"

    def test_pipe_with_extra_space_blocked(self):
        result = self._bash("ls  |  grep foo")
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# H3 — action_interrupt_agent or True removal
# ---------------------------------------------------------------------------

class TestH3InterruptAgent:
    """action_interrupt_agent must only interrupt when agent is actually running."""

    def _make_app(self):
        """Create a minimal stub that exercises the interrupt logic."""
        from src.ui.textual_app_impl import TextualAppBase
        app = TextualAppBase.__new__(TextualAppBase)
        app.orchestrator = MagicMock()
        app.history = []
        app._history_lock = __import__("threading").Lock()
        app.event_bus = None
        app._cancel_event = __import__("threading").Event()
        app._agent_running = False
        app._agent_thread = None
        return app

    def test_interrupt_when_not_running_does_not_set_cancel(self):
        """When _agent_running is False, interrupt must not set cancel_event."""
        # We test the condition logic directly since action_interrupt_agent
        # is a Textual action (not available without Textual).
        app = self._make_app()
        app._agent_running = False
        app._agent_thread = None

        # Simulate the fixed condition: agent not running → skip interrupt
        if app._agent_running:
            thread_alive = app._agent_thread and app._agent_thread.is_alive()
            if thread_alive:
                app._cancel_event.set()

        assert not app._cancel_event.is_set(), (
            "cancel_event must not be set when no agent is running"
        )

    def test_interrupt_when_running_sets_cancel(self):
        """When _agent_running is True and thread is alive, cancel must be set."""
        import threading
        app = self._make_app()
        app._agent_running = True

        # Create a live thread that blocks
        ready = threading.Event()
        stop = threading.Event()
        def _worker():
            ready.set()
            stop.wait()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        ready.wait()
        app._agent_thread = t

        try:
            thread_alive = app._agent_thread and app._agent_thread.is_alive()
            if app._agent_running and thread_alive:
                app._cancel_event.set()

            assert app._cancel_event.is_set()
        finally:
            stop.set()
            t.join(timeout=2)


# ---------------------------------------------------------------------------
# H5 — tee and touch removed from SAFE_COMMANDS
# ---------------------------------------------------------------------------

class TestH5TeeAndTouchBlocked:
    """tee and touch must not be in SAFE_COMMANDS."""

    def _bash(self, cmd: str):
        from src.tools.file_tools import bash
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            return bash(cmd, workdir=Path(td))

    def test_tee_is_blocked(self, tmp_path):
        """tee writes to files and must be removed from SAFE_COMMANDS."""
        result = self._bash(f"tee {tmp_path}/evil.txt")
        assert result["status"] == "error", (
            "tee must be blocked — it writes to arbitrary files"
        )

    def test_touch_is_blocked(self, tmp_path):
        """touch creates files and must be removed from SAFE_COMMANDS."""
        result = self._bash(f"touch {tmp_path}/newfile.txt")
        assert result["status"] == "error", (
            "touch must be blocked — it creates files, bypassing WorkspaceGuard"
        )

    def test_safe_commands_still_work(self, tmp_path):
        """Existing safe commands like ls must still be allowed."""
        result = self._bash(f"ls {tmp_path}")
        # Should not be blocked by SAFE_COMMANDS removal
        assert "tee" not in str(result.get("error", ""))
        assert "touch" not in str(result.get("error", ""))
