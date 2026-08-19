"""tests/unit/test_session_screen_revert_confirm.py — P2-2

Unit tests for:
- ``RevertConfirmScreen`` class (instantiation, attributes)
- ``SessionScreen.action_revert_session`` guard-clause paths (no selection,
  no git_sha, no working_dir)
- ``SessionScreen._do_revert`` subprocess logic (mocked)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — minimal stubs so we don't need a running Textual app
# ---------------------------------------------------------------------------


def _make_session_screen() -> "SessionScreen":
    """Return a SessionScreen with mocked Textual internals."""
    from tui.tui_src.ui.screens.session_screen import SessionScreen

    screen = object.__new__(SessionScreen)
    # Minimal attributes required by the action methods
    mock_app = SimpleNamespace(
        notify=MagicMock(),
        push_screen=MagicMock(),
    )
    object.__setattr__(screen, "_filtered", [])
    object.__setattr__(screen, "_selected", 0)
    object.__setattr__(screen, "_sessions", [])
    # Textual's `app` is a property; bypass it via the instance __dict__
    # by patching at the class level for this specific instance.
    screen.__class__ = type(
        "PatchedSessionScreen",
        (SessionScreen,),
        {"app": mock_app},
    )
    return screen


# ---------------------------------------------------------------------------
# RevertConfirmScreen
# ---------------------------------------------------------------------------


class TestRevertConfirmScreen:
    def test_instantiation_stores_sha_and_dir(self) -> None:
        from tui.tui_src.ui.screens.session_screen import RevertConfirmScreen

        rcs = RevertConfirmScreen("abc123def456", "/tmp/project")
        assert rcs._sha == "abc123def456"
        assert rcs._working_dir == "/tmp/project"

    def test_subclasses_modal_screen(self) -> None:
        from textual.screen import ModalScreen

        from tui.tui_src.ui.screens.session_screen import RevertConfirmScreen

        assert issubclass(RevertConfirmScreen, ModalScreen)


# ---------------------------------------------------------------------------
# action_revert_session — guard clauses
# ---------------------------------------------------------------------------


class TestActionRevertSessionGuards:
    def test_no_selection_notifies_warning(self) -> None:
        screen = _make_session_screen()
        screen._filtered = []  # empty list → guard fires
        screen.action_revert_session()
        screen.app.notify.assert_called_once()
        args = screen.app.notify.call_args
        assert "No session selected" in args[0][0]

    def test_selected_out_of_range_notifies_warning(self) -> None:
        screen = _make_session_screen()
        screen._filtered = [("p", {})]
        screen._selected = 5  # out of range
        screen.action_revert_session()
        screen.app.notify.assert_called_once()

    def test_no_git_sha_notifies_warning(self) -> None:
        screen = _make_session_screen()
        screen._filtered = [(Path("/tmp/s.json"), {"working_dir": "/tmp"})]
        screen._selected = 0
        screen.action_revert_session()
        screen.app.notify.assert_called_once()
        msg = screen.app.notify.call_args[0][0]
        assert "git snapshot" in msg.lower() or "No git snapshot" in msg

    def test_no_working_dir_notifies_warning(self) -> None:
        screen = _make_session_screen()
        screen._filtered = [(Path("/tmp/s.json"), {"git_sha": "abc123"})]
        screen._selected = 0
        screen.action_revert_session()
        screen.app.notify.assert_called_once()
        msg = screen.app.notify.call_args[0][0]
        assert "working directory" in msg.lower()

    def test_valid_session_pushes_confirm_screen(self) -> None:
        screen = _make_session_screen()
        screen._filtered = [
            (Path("/tmp/s.json"), {"git_sha": "deadbeef", "working_dir": "/tmp"})
        ]
        screen._selected = 0
        screen.action_revert_session()
        # push_screen should be called once with (RevertConfirmScreen, callback)
        screen.app.push_screen.assert_called_once()
        pushed_arg = screen.app.push_screen.call_args[0][0]
        from tui.tui_src.ui.screens.session_screen import RevertConfirmScreen
        assert isinstance(pushed_arg, RevertConfirmScreen)


# ---------------------------------------------------------------------------
# _do_revert — subprocess logic
# ---------------------------------------------------------------------------


class TestDoRevert:
    def _screen_with_dismiss(self) -> "SessionScreen":
        screen = _make_session_screen()
        screen.dismiss = MagicMock()
        return screen

    def test_success_notifies_sha_prefix(self, tmp_path: Path) -> None:
        screen = self._screen_with_dismiss()
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            screen._do_revert("abc123456789", str(tmp_path))

        screen.app.notify.assert_called_once()
        msg = screen.app.notify.call_args[0][0]
        assert "abc12345" in msg  # first 8 chars of sha

    def test_git_checkout_failure_notifies_error(self, tmp_path: Path) -> None:
        screen = self._screen_with_dismiss()

        def _side_effect(cmd, **kw):
            r = MagicMock()
            if "checkout" in cmd:
                r.returncode = 1
                r.stderr = "error: pathspec not found"
            else:
                r.returncode = 0
                r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=_side_effect):
            screen._do_revert("abc123", str(tmp_path))

        screen.app.notify.assert_called_once()
        call_kw = screen.app.notify.call_args[1]
        assert call_kw.get("severity") == "error"

    def test_git_not_found_notifies_error(self, tmp_path: Path) -> None:
        screen = self._screen_with_dismiss()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            screen._do_revert("abc123", str(tmp_path))
        msg = screen.app.notify.call_args[0][0]
        assert "git not found" in msg.lower()

    def test_missing_working_dir_notifies_error(self) -> None:
        screen = self._screen_with_dismiss()
        screen._do_revert("abc123", "/nonexistent/path/that/does/not/exist")
        screen.app.notify.assert_called_once()
        call_kw = screen.app.notify.call_args[1]
        assert call_kw.get("severity") == "error"

    def test_dismiss_called_on_success(self, tmp_path: Path) -> None:
        screen = self._screen_with_dismiss()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            screen._do_revert("abc123", str(tmp_path))
        screen.dismiss.assert_called_once()

    def test_dismiss_called_on_failure(self, tmp_path: Path) -> None:
        screen = self._screen_with_dismiss()

        def _fail(cmd, **kw):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "err"
            return r

        with patch("subprocess.run", side_effect=_fail):
            screen._do_revert("abc123", str(tmp_path))
        screen.dismiss.assert_called_once()
