"""Tests for ChatDisplayMixin (P1-6 Phase B).

Tests the logic-bearing helpers in isolation using a lightweight stub class.
Methods that only call self.query_one() / self.call_later() are tested for
correct delegation without a full Textual DOM.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub infrastructure
# ---------------------------------------------------------------------------


class _FakeWidget:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.display = True
        self._calls: list[str] = []

    def update(self, text: str) -> None:
        self._text = text
        self._calls.append(f"update:{text}")

    def update_picker(self, matches, index) -> None:
        self._calls.append(f"picker:{matches}:{index}")


class _FakeBridge:
    def __init__(self, wd: str = "/tmp") -> None:
        self.working_dir = wd


class _StubApp:
    """Minimal host that satisfies ChatDisplayMixin's attribute contract."""

    def __init__(self) -> None:
        self.is_streaming: bool = False
        self.active_role: str = "lead_architect"
        self.agent_running: bool = False
        self._current_stream = None
        self._tool_widgets: dict[str, Any] = {}
        self._tool_args: dict[str, Any] = {}
        self._at_file_cache: list[str] = []
        self._at_file_cache_ts: float = 0.0
        self._at_picker_active: bool = False
        self._at_picker_matches: list[str] = []
        self._at_picker_index: int = 0
        self._at_picker_widget = None
        self._at_prefix: str = ""
        self._palette_active: bool = False
        self._palette_matches: list[str] = []
        self._palette_index: int = 0
        self._bridge = _FakeBridge()
        self._later_calls: list[tuple] = []
        self._notified: list[str] = []

    def call_later(self, fn, *args) -> None:
        self._later_calls.append((fn, args))

    def notify(self, msg: str, severity: str = "information", **kw) -> None:
        self._notified.append(msg)

    def query_one(self, selector: str, widget_type=None):
        raise Exception(f"no DOM in tests: {selector}")

    def _update_status_bar(self) -> None:
        pass  # provided by StatusBarMixin in production


from tui.tui_src.ui.components.chat_mixin import ChatDisplayMixin


class _App(_StubApp, ChatDisplayMixin):
    pass


# ---------------------------------------------------------------------------
# _finalize_stream
# ---------------------------------------------------------------------------


class TestFinalizeStream:
    def test_clears_current_stream(self):
        app = _App()
        app._current_stream = MagicMock()
        app._finalize_stream()
        assert app._current_stream is None

    def test_sets_is_streaming_false(self):
        app = _App()
        app._current_stream = MagicMock()
        app.is_streaming = True
        app._finalize_stream()
        assert app.is_streaming is False

    def test_noop_when_no_stream(self):
        app = _App()
        app._current_stream = None
        app.is_streaming = False
        app._finalize_stream()  # must not raise
        assert app.is_streaming is False


# ---------------------------------------------------------------------------
# _clear_chat_panel
# ---------------------------------------------------------------------------


class TestClearChatPanel:
    def test_clears_tool_widgets(self):
        app = _App()
        app._tool_widgets["x"] = MagicMock()
        app._clear_chat_panel()
        assert app._tool_widgets == {}

    def test_clears_tool_args(self):
        app = _App()
        app._tool_args["x"] = {"a": 1}
        app._clear_chat_panel()
        assert app._tool_args == {}

    def test_finalizes_stream(self):
        app = _App()
        app._current_stream = MagicMock()
        app.is_streaming = True
        app._clear_chat_panel()
        assert app._current_stream is None
        assert app.is_streaming is False


# ---------------------------------------------------------------------------
# _at_picker_hide
# ---------------------------------------------------------------------------


class TestAtPickerHide:
    def test_resets_state(self):
        app = _App()
        app._at_picker_active = True
        app._at_picker_matches = ["foo.py"]
        app._at_prefix = "@foo"
        widget = _FakeWidget()
        app._at_picker_widget = widget
        app._at_picker_hide()
        assert app._at_picker_active is False
        assert app._at_picker_matches == []
        assert app._at_prefix == ""
        assert widget.display is False

    def test_no_widget_does_not_raise(self):
        app = _App()
        app._at_picker_widget = None
        app._at_picker_hide()  # must not raise


# ---------------------------------------------------------------------------
# _at_picker_navigate
# ---------------------------------------------------------------------------


class TestAtPickerNavigate:
    def setup_method(self):
        self.app = _App()
        self.app._at_picker_matches = ["a.py", "b.py", "c.py"]
        self.app._at_picker_index = 1

    def test_navigate_up(self):
        self.app._at_picker_navigate("up")
        assert self.app._at_picker_index == 0

    def test_navigate_down(self):
        self.app._at_picker_navigate("down")
        assert self.app._at_picker_index == 2

    def test_clamp_at_zero(self):
        self.app._at_picker_index = 0
        self.app._at_picker_navigate("up")
        assert self.app._at_picker_index == 0

    def test_clamp_at_max(self):
        self.app._at_picker_index = 2
        self.app._at_picker_navigate("down")
        assert self.app._at_picker_index == 2

    def test_updates_widget(self):
        widget = _FakeWidget()
        self.app._at_picker_widget = widget
        self.app._at_picker_navigate("down")
        assert any("picker" in c for c in widget._calls)

    def test_noop_when_no_matches(self):
        app = _App()
        app._at_picker_navigate("down")  # must not raise


# ---------------------------------------------------------------------------
# _palette_navigate
# ---------------------------------------------------------------------------


class TestPaletteNavigate:
    def setup_method(self):
        self.app = _App()
        self.app._palette_matches = ["/clear", "/help", "/new"]
        self.app._palette_index = 1

    def test_navigate_up(self):
        self.app._palette_navigate("up")
        assert self.app._palette_index == 0

    def test_navigate_down(self):
        self.app._palette_navigate("down")
        assert self.app._palette_index == 2

    def test_clamp_at_zero(self):
        self.app._palette_index = 0
        self.app._palette_navigate("up")
        assert self.app._palette_index == 0

    def test_clamp_at_max(self):
        self.app._palette_index = 2
        self.app._palette_navigate("down")
        assert self.app._palette_index == 2

    def test_noop_when_no_matches(self):
        app = _App()
        app._palette_navigate("down")  # must not raise


# ---------------------------------------------------------------------------
# _palette_complete
# ---------------------------------------------------------------------------


class TestPaletteComplete:
    def test_returns_selected_command(self):
        app = _App()
        app._palette_matches = ["/clear", "/help"]
        app._palette_index = 1
        result = app._palette_complete()
        assert result == "/help"

    def test_resets_palette_state(self):
        app = _App()
        app._palette_matches = ["/clear"]
        app._palette_index = 0
        app._palette_active = True
        app._palette_complete()
        assert app._palette_active is False
        assert app._palette_matches == []
        assert app._palette_index == 0

    def test_returns_empty_string_when_no_matches(self):
        app = _App()
        result = app._palette_complete()
        assert result == ""


# ---------------------------------------------------------------------------
# _expand_at_tokens
# ---------------------------------------------------------------------------


class TestExpandAtTokens:
    def test_no_at_tokens_unchanged(self):
        app = _App()
        text = "Hello world"
        assert app._expand_at_tokens(text) == text

    def test_expands_existing_file(self):
        with tempfile.TemporaryDirectory() as wd:
            fpath = Path(wd) / "notes.txt"
            fpath.write_text("file content here")
            app = _App()
            app._bridge = _FakeBridge(wd)
            result = app._expand_at_tokens("Check @notes.txt please")
            assert "<file: notes.txt>" in result
            assert "file content here" in result

    def test_leaves_nonexistent_file_unchanged(self):
        with tempfile.TemporaryDirectory() as wd:
            app = _App()
            app._bridge = _FakeBridge(wd)
            result = app._expand_at_tokens("Look @missing.py")
            assert "@missing.py" in result
            assert "<file:" not in result

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as wd:
            # Write a file outside wd
            outside = Path(wd).parent / "secret.txt"
            outside.write_text("secret")
            app = _App()
            app._bridge = _FakeBridge(wd)
            result = app._expand_at_tokens("Read @../secret.txt")
            assert "<file:" not in result
            outside.unlink(missing_ok=True)

    def test_bridge_error_returns_original(self):
        app = _App()
        app._bridge = _FakeBridge("")  # empty working_dir → os.getcwd() fallback
        result = app._expand_at_tokens("No expansion @foo")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _list_workspace_files
# ---------------------------------------------------------------------------


class TestListWorkspaceFiles:
    def test_returns_files_in_workspace(self):
        with tempfile.TemporaryDirectory() as wd:
            (Path(wd) / "alpha.py").write_text("")
            (Path(wd) / "beta.txt").write_text("")
            app = _App()
            app._bridge = _FakeBridge(wd)
            files = app._list_workspace_files()
            assert "alpha.py" in files
            assert "beta.txt" in files

    def test_query_filters_results(self):
        with tempfile.TemporaryDirectory() as wd:
            (Path(wd) / "alpha.py").write_text("")
            (Path(wd) / "beta.txt").write_text("")
            app = _App()
            app._bridge = _FakeBridge(wd)
            files = app._list_workspace_files("alpha")
            assert all("alpha" in f for f in files)
            assert "beta.txt" not in files

    def test_cache_is_used_within_60s(self):
        app = _App()
        app._bridge = _FakeBridge("/nonexistent_dir_xyz")
        app._at_file_cache = ["cached.py"]
        app._at_file_cache_ts = 9e18  # far future → cache always valid
        files = app._list_workspace_files()
        assert "cached.py" in files

    def test_skips_hidden_dirs(self):
        with tempfile.TemporaryDirectory() as wd:
            git_dir = Path(wd) / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("")
            (Path(wd) / "main.py").write_text("")
            app = _App()
            app._bridge = _FakeBridge(wd)
            files = app._list_workspace_files()
            assert not any(".git" in f for f in files)
            assert "main.py" in files

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as wd:
            app = _App()
            app._bridge = _FakeBridge(wd)
            files = app._list_workspace_files()
            assert files == []

    def test_bad_working_dir_returns_empty(self):
        app = _App()
        app._bridge = _FakeBridge("/this_path_does_not_exist_xyz")
        files = app._list_workspace_files()
        assert files == []


# ---------------------------------------------------------------------------
# _chat_handle_usage_turn_summary
# ---------------------------------------------------------------------------


class TestChatHandleUsageTurnSummary:
    def _make_event(self, input_tokens=0, output_tokens=0, cost_usd=0.0):
        ev = MagicMock()
        ev.input_tokens = input_tokens
        ev.output_tokens = output_tokens
        ev.cost_usd = cost_usd
        return ev

    def _app_with_chat_log(self):
        """Return an app stub whose query_one returns a fake chat_log."""
        app = _App()
        fake_log = MagicMock()
        app.query_one = MagicMock(return_value=fake_log)
        return app

    def test_schedules_widget_with_tokens(self):
        app = self._app_with_chat_log()
        ev = self._make_event(input_tokens=100, output_tokens=50)
        app._chat_handle_usage_turn_summary(ev)
        assert len(app._later_calls) > 0

    def test_noop_when_no_tokens_no_cost(self):
        app = self._app_with_chat_log()
        ev = self._make_event()
        app._chat_handle_usage_turn_summary(ev)
        assert len(app._later_calls) == 0

    def test_schedules_widget_with_cost(self):
        app = self._app_with_chat_log()
        ev = self._make_event(cost_usd=0.005)
        app._chat_handle_usage_turn_summary(ev)
        assert len(app._later_calls) > 0


# ---------------------------------------------------------------------------
# _chat_handle_stream_chunk / _chat_handle_thinking_update
# (light tests — no DOM needed, just verify _ensure_stream_widget is called)
# ---------------------------------------------------------------------------


class TestChatHandleStreamChunk:
    def test_finalizes_stream_on_complete_chunk(self):
        app = _App()
        stream = MagicMock()
        app._current_stream = stream
        fake_log = MagicMock()
        app.query_one = MagicMock(return_value=fake_log)

        ev = MagicMock()
        ev.chunk = "hello"
        ev.is_partial = False

        app._chat_handle_stream_chunk(ev)

        assert app._current_stream is None  # finalized
        stream.append_chunk.assert_called_once_with("hello")

    def test_does_not_finalize_on_partial_chunk(self):
        app = _App()
        stream = MagicMock()
        app._current_stream = stream
        fake_log = MagicMock()
        app.query_one = MagicMock(return_value=fake_log)

        ev = MagicMock()
        ev.chunk = "part"
        ev.is_partial = True

        app._chat_handle_stream_chunk(ev)

        assert app._current_stream is stream  # NOT finalized


class TestChatHandleThinkingUpdate:
    def test_finalizes_on_complete(self):
        app = _App()
        stream = MagicMock()
        app._current_stream = stream

        ev = MagicMock()
        ev.content = "thinking..."
        ev.is_complete = True

        app._chat_handle_thinking_update(ev)
        assert app._current_stream is None

    def test_no_finalize_when_not_complete(self):
        app = _App()
        stream = MagicMock()
        app._current_stream = stream

        ev = MagicMock()
        ev.content = "..."
        ev.is_complete = False

        app._chat_handle_thinking_update(ev)
        assert app._current_stream is stream
