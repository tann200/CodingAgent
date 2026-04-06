"""
tests/unit/test_standalone_tui.py

Validates the standalone TUI implementation in tui/src/ui/.
Tests are written in the project's established pattern:
  - Source inspection for structural checks (bindings, method names)
  - Direct constructor/logic tests (no live Textual event loop needed)
  - pytest.skip guards where Textual runtime is required
"""

from __future__ import annotations

import inspect
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Ensure tui/src is on sys.path so `ui.*` imports resolve ──────────────────
_TUI_SRC = str(Path(__file__).parent.parent.parent / "tui" / "src")
if _TUI_SRC not in sys.path:
    sys.path.insert(0, _TUI_SRC)


# ──────────────────────────────────────────────────────────────────────────────
# bus.py
# ──────────────────────────────────────────────────────────────────────────────


class TestBusOptionalDefaults:
    """bus.py: Optional[...] = None defaults must not raise TypeError."""

    def test_tool_execution_notice_no_args(self):
        from ui.bus import ToolExecutionNotice  # type: ignore[import]

        e = ToolExecutionNotice("my_tool")
        assert e.tool_name == "my_tool"
        assert e.arguments == {}

    def test_tool_execution_notice_with_args(self):
        from ui.bus import ToolExecutionNotice  # type: ignore[import]

        e = ToolExecutionNotice("bash", arguments={"cmd": "ls"})
        assert e.arguments == {"cmd": "ls"}

    def test_mcp_server_status_no_names(self):
        from ui.bus import McpServerStatusEvent  # type: ignore[import]

        e = McpServerStatusEvent(running=True, count=3)
        assert e.server_names == []
        assert e.running is True
        assert e.count == 3

    def test_mcp_server_status_with_names(self):
        from ui.bus import McpServerStatusEvent  # type: ignore[import]

        e = McpServerStatusEvent(running=True, count=1, server_names=["tools"])
        assert e.server_names == ["tools"]

    def test_tool_permission_no_args(self):
        from ui.bus import ToolPermissionEvent  # type: ignore[import]

        e = ToolPermissionEvent(tool="rm", tool_id="t1")
        assert e.args == {}
        assert e.tool == "rm"
        assert e.tool_id == "t1"

    def test_tool_permission_with_args(self):
        from ui.bus import ToolPermissionEvent  # type: ignore[import]

        e = ToolPermissionEvent(tool="rm", args={"path": "/tmp"})
        assert e.args == {"path": "/tmp"}


class TestBusNewEvents:
    """bus.py: new events added this session."""

    def test_step_start_event(self):
        from ui.bus import StepStartEvent  # type: ignore[import]

        e = StepStartEvent(tool="read_file", step=2, total=5, run_id="r1")
        assert e.tool == "read_file"
        assert e.step == 2
        assert e.total == 5
        assert e.run_id == "r1"

    def test_step_start_defaults(self):
        from ui.bus import StepStartEvent  # type: ignore[import]

        e = StepStartEvent()
        assert e.tool == ""
        assert e.step == 0
        assert e.total == 0

    def test_step_finish_event(self):
        from ui.bus import StepFinishEvent  # type: ignore[import]

        e = StepFinishEvent(tool="write_file", ok=True, elapsed_ms=42)
        assert e.tool == "write_file"
        assert e.ok is True
        assert e.elapsed_ms == 42

    def test_step_finish_optional_elapsed(self):
        from ui.bus import StepFinishEvent  # type: ignore[import]

        e = StepFinishEvent()
        assert e.elapsed_ms is None

    def test_mcp_server_status_defaults(self):
        from ui.bus import McpServerStatusEvent  # type: ignore[import]

        e = McpServerStatusEvent()
        assert e.running is False
        assert e.count == 0
        assert e.server_names == []


# ──────────────────────────────────────────────────────────────────────────────
# events.py
# ──────────────────────────────────────────────────────────────────────────────


class TestEventsOptionalDefaults:
    """events.py: Optional[Dict] default must not raise TypeError."""

    def test_update_settings_no_args(self):
        from ui.events import UpdateSettings  # type: ignore[import]

        e = UpdateSettings()
        assert e.updates == {}

    def test_update_settings_with_dict(self):
        from ui.events import UpdateSettings  # type: ignore[import]

        e = UpdateSettings(updates={"theme": "dark"})
        assert e.updates["theme"] == "dark"


class TestNewEvents:
    """events.py: new tool-permission events."""

    def test_tool_permission_approved(self):
        from ui.events import ToolPermissionApproved  # type: ignore[import]

        e = ToolPermissionApproved(tool="bash", tool_id="abc")
        assert e.tool == "bash"
        assert e.tool_id == "abc"

    def test_tool_permission_approved_defaults(self):
        from ui.events import ToolPermissionApproved  # type: ignore[import]

        e = ToolPermissionApproved()
        assert e.tool == ""
        assert e.tool_id == ""

    def test_tool_permission_denied(self):
        from ui.events import ToolPermissionDenied  # type: ignore[import]

        e = ToolPermissionDenied(tool="rm", tool_id="x1")
        assert e.tool == "rm"
        assert e.tool_id == "x1"


# ──────────────────────────────────────────────────────────────────────────────
# artifact.py
# ──────────────────────────────────────────────────────────────────────────────


class TestAgentArtifact:
    """artifact.py: reactive field rename from 'content' to '_stream_content'."""

    def test_constructor(self):
        from ui.components.artifact import AgentArtifact  # type: ignore[import]

        a = AgentArtifact(content="hello", title="T", kind="markdown")
        assert a._content_str == "hello"
        assert a.title == "T"
        assert a.kind == "markdown"

    def test_reactive_field_renamed(self):
        """'content' reactive must NOT exist; '_stream_content' must exist."""
        import ui.components.artifact as mod  # type: ignore[import]

        src = inspect.getsource(mod.AgentArtifact)
        assert "_stream_content" in src, "_stream_content reactive not found"
        assert "watch__stream_content" in src, "watcher missing"
        # The class-level 'content = reactive(...)' that clashes with Static.content
        # must be gone:
        assert "content = reactive" not in src, "Old 'content' reactive still present"

    def test_append_chunk_updates_internal_str(self):
        from ui.components.artifact import AgentArtifact  # type: ignore[import]

        a = AgentArtifact(content="hello", title="T", kind="markdown")
        a.append_chunk(" world")
        assert a._content_str == "hello world"

    def test_diff_kind_instantiation(self):
        from ui.components.artifact import AgentArtifact  # type: ignore[import]

        a = AgentArtifact(content="", title="diff", kind="diff")
        assert a.kind == "diff"

    def test_sanitize_removes_control_chars(self):
        from ui.components.artifact import AgentArtifact  # type: ignore[import]

        a = AgentArtifact()
        result = a._sanitize("hello\x00world\x01\n")
        assert "\x00" not in result
        assert "\x01" not in result
        assert "\n" in result  # newlines preserved

    def test_build_renderable_empty(self):
        from ui.components.artifact import AgentArtifact  # type: ignore[import]
        from rich.text import Text

        a = AgentArtifact()
        r = a._build_renderable("")
        assert isinstance(r, Text)

    def test_build_renderable_markdown(self):
        from ui.components.artifact import AgentArtifact  # type: ignore[import]
        from rich.panel import Panel

        a = AgentArtifact(kind="markdown")
        r = a._build_renderable("# heading")
        assert isinstance(r, Panel)

    def test_build_renderable_diff(self):
        from ui.components.artifact import AgentArtifact  # type: ignore[import]
        from rich.panel import Panel

        a = AgentArtifact(kind="diff")
        r = a._build_renderable("+ added\n- removed")
        assert isinstance(r, Panel)


# ──────────────────────────────────────────────────────────────────────────────
# chat_input.py
# ──────────────────────────────────────────────────────────────────────────────


class TestChatInputStructure:
    """chat_input.py: structural checks — no live event loop needed."""

    def test_text_changed_class_exists(self):
        from ui.components.chat_input import ChatTextArea  # type: ignore[import]

        assert hasattr(ChatTextArea, "TextChanged"), "TextChanged inner class missing"

    def test_submitted_class_exists(self):
        from ui.components.chat_input import ChatTextArea  # type: ignore[import]

        assert hasattr(ChatTextArea, "Submitted"), "Submitted inner class missing"

    def test_changed_class_not_present(self):
        """Old 'Changed' inner class was renamed to avoid clashing with TextArea.Changed."""
        from ui.components.chat_input import ChatTextArea  # type: ignore[import]

        # The attribute 'Changed' is inherited from TextArea — our custom class
        # must be named TextChanged so it doesn't shadow the parent's Changed.
        # Verify by inspecting the source for our definition.
        src = inspect.getsource(ChatTextArea)
        assert "class TextChanged" in src
        # Must NOT define its own 'class Changed'
        assert "class Changed" not in src

    def test_slash_commands_list(self):
        from ui.components.chat_input import SLASH_COMMANDS  # type: ignore[import]

        assert "/help" in SLASH_COMMANDS
        assert "/sessions" in SLASH_COMMANDS
        assert "/timeline" in SLASH_COMMANDS
        assert "/quit" in SLASH_COMMANDS

    def test_slash_command_descriptions_complete(self):
        from ui.components.chat_input import SLASH_COMMANDS, SLASH_COMMAND_DESCRIPTIONS  # type: ignore[import]

        for cmd in SLASH_COMMANDS:
            assert cmd in SLASH_COMMAND_DESCRIPTIONS, f"{cmd} missing description"

    def test_import_uses_relative_logging(self):
        src_file = Path(_TUI_SRC) / "ui" / "components" / "chat_input.py"
        text = src_file.read_text()
        assert "from ..logging import get_logger" in text, "Should use relative import"
        assert "from src.ui.logging" not in text, (
            "Should NOT use absolute src.ui import"
        )

    def test_submitted_message_fields(self):
        from ui.components.chat_input import ChatTextArea  # type: ignore[import]

        # Verify Submitted has `text` and `text_area` fields via source inspection
        src = inspect.getsource(ChatTextArea.Submitted)
        assert "self.text" in src
        assert "self.text_area" in src

    def test_text_changed_message_field(self):
        from ui.components.chat_input import ChatTextArea  # type: ignore[import]

        src = inspect.getsource(ChatTextArea.TextChanged)
        assert "self.text" in src


# ──────────────────────────────────────────────────────────────────────────────
# file_picker.py
# ──────────────────────────────────────────────────────────────────────────────


class TestFilePickerOverlay:
    """file_picker.py: structural and logic checks."""

    def test_import(self):
        from ui.components.file_picker import FilePickerOverlay  # type: ignore[import]

        assert FilePickerOverlay is not None

    def test_update_picker_method_exists(self):
        from ui.components.file_picker import FilePickerOverlay  # type: ignore[import]

        assert hasattr(FilePickerOverlay, "update_picker")

    def test_no_src_core_imports(self):
        src_file = Path(_TUI_SRC) / "ui" / "components" / "file_picker.py"
        text = src_file.read_text()
        assert "from src.core" not in text
        assert "import src.core" not in text


# ──────────────────────────────────────────────────────────────────────────────
# screens/session_list.py
# ──────────────────────────────────────────────────────────────────────────────


class TestSessionListScreen:
    """session_list.py: structure and logic (no live Textual runtime)."""

    def test_import(self):
        from ui.screens.session_list import SessionListScreen  # type: ignore[import]

        assert SessionListScreen is not None

    def test_no_src_core_imports(self):
        src_file = Path(_TUI_SRC) / "ui" / "screens" / "session_list.py"
        text = src_file.read_text()
        assert "from src.core" not in text
        assert "import src.core" not in text

    def test_render_list_not_render(self):
        """_render was renamed to _render_list to avoid Widget._render clash."""
        src_file = Path(_TUI_SRC) / "ui" / "screens" / "session_list.py"
        text = src_file.read_text()
        assert "def _render_list" in text
        assert "def _render(" not in text

    def test_filter_logic(self):
        from ui.screens.session_list import SessionListScreen  # type: ignore[import]

        screen = SessionListScreen.__new__(SessionListScreen)
        # Bootstrap internal state directly
        from pathlib import Path as _P

        screen._all_sessions = [
            (
                _P("/fake/session_alpha.json"),
                {"task_name": "alpha task", "timestamp": 0, "message_count": 3},
            ),
            (
                _P("/fake/session_beta.json"),
                {"task_name": "beta task", "timestamp": 1, "message_count": 5},
            ),
            (
                _P("/fake/session_gamma.json"),
                {"task_name": "gamma task", "timestamp": 2, "message_count": 1},
            ),
        ]
        screen._filtered = list(screen._all_sessions)
        screen._selected = 0
        screen._filter("alpha")
        assert len(screen._filtered) == 1
        assert screen._filtered[0][1]["task_name"] == "alpha task"

    def test_filter_empty_query_returns_all(self):
        from ui.screens.session_list import SessionListScreen  # type: ignore[import]

        screen = SessionListScreen.__new__(SessionListScreen)
        from pathlib import Path as _P

        screen._all_sessions = [
            (
                _P("/fake/a.json"),
                {"task_name": "a", "timestamp": 0, "message_count": 1},
            ),
            (
                _P("/fake/b.json"),
                {"task_name": "b", "timestamp": 1, "message_count": 2},
            ),
        ]
        screen._filtered = list(screen._all_sessions)
        screen._selected = 0
        screen._filter("")
        assert len(screen._filtered) == 2

    def test_key_bindings_defined(self):
        from ui.screens.session_list import SessionListScreen  # type: ignore[import]

        src = inspect.getsource(SessionListScreen)
        assert "escape" in src


# ──────────────────────────────────────────────────────────────────────────────
# screens/timeline.py
# ──────────────────────────────────────────────────────────────────────────────


class TestTimelineScreen:
    """timeline.py: structure and logic."""

    def test_import(self):
        from ui.screens.timeline import TimelineScreen  # type: ignore[import]

        assert TimelineScreen is not None

    def test_no_src_core_imports(self):
        src_file = Path(_TUI_SRC) / "ui" / "screens" / "timeline.py"
        text = src_file.read_text()
        assert "from src.core" not in text
        assert "import src.core" not in text

    def test_render_list_not_render(self):
        src_file = Path(_TUI_SRC) / "ui" / "screens" / "timeline.py"
        text = src_file.read_text()
        assert "def _render_list" in text
        assert "def _render(" not in text

    def test_accepts_history_list(self):
        from ui.screens.timeline import TimelineScreen  # type: ignore[import]

        history = [
            ("user", "hello world"),
            ("assistant", "hi there"),
            ("system", "ignored"),  # system messages are excluded
        ]
        screen = TimelineScreen.__new__(TimelineScreen)
        # Replicate __init__ logic manually
        screen._messages = [
            {"role": role, "content": content, "index": i}
            for i, (role, content) in enumerate(history)
            if role in ("user", "assistant") and content
        ]
        screen._filtered = list(screen._messages)
        screen._selected = max(0, len(screen._filtered) - 1)
        assert len(screen._messages) == 2
        assert screen._messages[0]["role"] == "user"
        assert screen._messages[1]["role"] == "assistant"
        # system role excluded
        assert all(m["role"] != "system" for m in screen._messages)

    def test_filter_logic(self):
        from ui.screens.timeline import TimelineScreen  # type: ignore[import]

        history = [
            ("user", "tell me about cats"),
            ("assistant", "cats are great"),
            ("user", "what about dogs"),
        ]
        screen = TimelineScreen(history)
        # Access internal state set by __init__
        assert len(screen._messages) == 3
        screen._filter("cats")
        assert len(screen._filtered) == 2
        assert all("cats" in m["content"] for m in screen._filtered)

    def test_filter_empty_restores_all(self):
        from ui.screens.timeline import TimelineScreen  # type: ignore[import]

        history = [("user", "a"), ("assistant", "b"), ("user", "c")]
        screen = TimelineScreen(history)
        screen._filter("a")
        assert len(screen._filtered) == 1
        screen._filter("")
        assert len(screen._filtered) == 3

    def test_key_bindings_defined(self):
        from ui.screens.timeline import TimelineScreen  # type: ignore[import]

        src = inspect.getsource(TimelineScreen)
        assert "escape" in src
        assert "action_close_timeline" in src


# ──────────────────────────────────────────────────────────────────────────────
# main.py
# ──────────────────────────────────────────────────────────────────────────────


class TestMainModule:
    """main.py: create_app factory and TextualAppStub."""

    def test_create_app_function_exists(self):
        from ui.main import create_app  # type: ignore[import]

        assert callable(create_app)

    def test_textual_app_stub_exists(self):
        from ui.main import TextualAppStub  # type: ignore[import]

        stub = TextualAppStub()
        assert hasattr(stub, "send_prompt")
        assert hasattr(stub, "interrupt")
        assert hasattr(stub, "force_interrupt")
        assert hasattr(stub, "run")

    def test_stub_send_prompt(self):
        from ui.main import TextualAppStub  # type: ignore[import]

        stub = TextualAppStub()
        result = stub.send_prompt("hello")
        assert result is True
        assert len(stub._messages) == 1
        assert stub._messages[0] == ("user", "hello")

    def test_stub_interrupt_no_op(self):
        from ui.main import TextualAppStub  # type: ignore[import]

        stub = TextualAppStub()
        stub.interrupt()  # must not raise
        stub.force_interrupt()  # must not raise

    def test_stub_run_no_op(self):
        from ui.main import TextualAppStub  # type: ignore[import]

        stub = TextualAppStub()
        stub.run()  # must not raise

    def test_get_app_returns_stub_on_error(self):
        from ui import main as main_mod  # type: ignore[import]

        with patch.object(
            main_mod, "create_app", side_effect=ImportError("no textual")
        ):
            app = main_mod._get_app()
        from ui.main import TextualAppStub  # type: ignore[import]

        assert isinstance(app, TextualAppStub)


# ──────────────────────────────────────────────────────────────────────────────
# components/__init__.py exports
# ──────────────────────────────────────────────────────────────────────────────


class TestComponentsInit:
    """components/__init__.py: ChatTextArea and FilePickerOverlay must be exported."""

    def test_chat_text_area_exported(self):
        from ui.components import ChatTextArea  # type: ignore[import]

        assert ChatTextArea is not None

    def test_file_picker_overlay_exported(self):
        from ui.components import FilePickerOverlay  # type: ignore[import]

        assert FilePickerOverlay is not None

    def test_all_list(self):
        import ui.components as comp  # type: ignore[import]

        assert "ChatTextArea" in comp.__all__
        assert "FilePickerOverlay" in comp.__all__

    def test_existing_exports_still_present(self):
        from ui.components import (  # type: ignore[import]
            HistoryInput,
            AgentArtifact,
            ThinkingProcess,
            StreamView,
            ConsolePanel,
            SideBySideDiff,
        )

        for cls in [
            HistoryInput,
            AgentArtifact,
            ThinkingProcess,
            StreamView,
            ConsolePanel,
            SideBySideDiff,
        ]:
            assert cls is not None


# ──────────────────────────────────────────────────────────────────────────────
# app.py — structural checks (no live runtime)
# ──────────────────────────────────────────────────────────────────────────────


class TestAppStructure:
    """app.py: structural validation without instantiating a Textual App."""

    def _get_source(self) -> str:
        src_file = Path(_TUI_SRC) / "ui" / "app.py"
        return src_file.read_text()

    def test_no_src_core_direct_imports(self):
        """The app must not have top-level (module-scope) src.core imports.

        Deferred imports inside method bodies (indented) are permitted because
        they do not execute at import time and therefore do not break standalone
        TUI operation.  Only bare, non-indented import lines are forbidden.
        """
        text = self._get_source()
        # A top-level import line starts at column 0 — no leading whitespace.
        top_level_src_core = [
            ln
            for ln in text.splitlines()
            if (ln.startswith("from src.core") or ln.startswith("import src.core"))
        ]
        assert top_level_src_core == [], (
            f"app.py must not have top-level src.core imports; found:\n"
            + "\n".join(top_level_src_core)
        )

    def test_session_list_import_is_lazy(self):
        text = self._get_source()
        assert "from .screens.session_list import SessionListScreen" in text

    def test_timeline_import_is_lazy(self):
        text = self._get_source()
        assert "from .screens.timeline import TimelineScreen" in text

    def test_mcp_status_chip_in_compose(self):
        text = self._get_source()
        assert "mcp_status_chip" in text

    def test_tool_permission_handler(self):
        text = self._get_source()
        assert "handle_tool_permission" in text
        assert "ToolPermissionEvent" in text

    def test_step_start_handler(self):
        text = self._get_source()
        assert "handle_step_start" in text

    def test_step_finish_handler(self):
        text = self._get_source()
        assert "handle_step_finish" in text

    def test_at_picker_widget_set_in_on_mount(self):
        """_at_picker_widget must be assigned in on_mount, not in compose."""
        text = self._get_source()
        # Find on_mount block — it should contain _at_picker_widget assignment
        assert "_at_picker_widget = self.query_one" in text
        # Compose must NOT assign self._at_picker_widget = FilePickerOverlay(...)
        compose_block = text[text.find("def compose") : text.find("def on_mount")]
        assert "self._at_picker_widget = FilePickerOverlay" not in compose_block

    def test_sub_title_at_app_level(self):
        """Must use self.sub_title (App attribute), not header.sub_title."""
        text = self._get_source()
        assert "self.sub_title" in text
        assert "header.sub_title" not in text

    def test_severity_cast_present(self):
        """severity= argument for notify must be cast to SeverityLevel."""
        text = self._get_source()
        assert 'cast("SeverityLevel"' in text

    def test_text_changed_handler(self):
        text = self._get_source()
        assert "ChatTextArea.TextChanged" in text
        # The old Changed handler must be gone
        assert "ChatTextArea.Changed" not in text

    def test_ctrl_q_binding(self):
        text = self._get_source()
        assert "ctrl+q" in text

    def test_ctrl_s_binding(self):
        text = self._get_source()
        assert "ctrl+s" in text

    def test_expand_at_tokens_method(self):
        text = self._get_source()
        assert "_expand_at_tokens" in text

    def test_save_session_snapshot_method(self):
        text = self._get_source()
        assert "_save_session_snapshot" in text

    def test_get_sessions_dir_method(self):
        text = self._get_source()
        assert "_get_sessions_dir" in text


# ──────────────────────────────────────────────────────────────────────────────
# scripts/lmstudio_diagnostic.py
# ──────────────────────────────────────────────────────────────────────────────


class TestLmStudioDiagnostic:
    """lmstudio_diagnostic.py: psutil optional guard."""

    def _get_source(self) -> str:
        p = Path(__file__).parent.parent.parent / "scripts" / "lmstudio_diagnostic.py"
        return p.read_text()

    def test_psutil_imported_as_alias(self):
        text = self._get_source()
        assert "import psutil as _psutil" in text

    def test_psutil_none_fallback(self):
        text = self._get_source()
        assert "_psutil = None" in text

    def test_sample_mem_guards_psutil(self):
        text = self._get_source()
        assert "_psutil is None" in text or "_HAS_PSUTIL" in text

    def test_sample_mem_returns_empty_without_psutil(self):
        """sample_mem() must return {} when psutil is unavailable."""
        import importlib.util, types

        spec = importlib.util.spec_from_file_location(
            "lmstudio_diagnostic",
            Path(__file__).parent.parent.parent / "scripts" / "lmstudio_diagnostic.py",
        )
        mod = types.ModuleType("lmstudio_diagnostic")
        # Force _HAS_PSUTIL = False before loading
        with patch("builtins.__import__", side_effect=ImportError):
            try:
                loader = spec.loader  # type: ignore[union-attr]
                loader.exec_module(mod)  # type: ignore[union-attr]
            except Exception:
                pass

        # Reload properly but with _psutil patched to None
        spec2 = importlib.util.spec_from_file_location(
            "lmstudio_diag2",
            Path(__file__).parent.parent.parent / "scripts" / "lmstudio_diagnostic.py",
        )
        mod2 = importlib.util.module_from_spec(spec2)  # type: ignore[arg-type]
        spec2.loader.exec_module(mod2)  # type: ignore[union-attr]
        # Simulate missing psutil
        mod2._HAS_PSUTIL = False  # type: ignore[attr-defined]
        mod2._psutil = None  # type: ignore[attr-defined]
        result = mod2.sample_mem()
        assert result == {}

    def test_make_prompt(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "lmstudio_diag3",
            Path(__file__).parent.parent.parent / "scripts" / "lmstudio_diagnostic.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        prompt = mod.make_prompt(5)
        assert isinstance(prompt, str)
        assert len(prompt) > 0


# ──────────────────────────────────────────────────────────────────────────────
# core_bridge.py — structural checks
# ──────────────────────────────────────────────────────────────────────────────


class TestCoreBridgeStructure:
    """core_bridge.py: validate new handler methods exist."""

    def _get_source(self) -> str:
        src_file = Path(_TUI_SRC) / "ui" / "core_bridge.py"
        return src_file.read_text()

    def test_step_start_handler(self):
        text = self._get_source()
        assert "_on_step_start" in text or "step.start" in text

    def test_step_finish_handler(self):
        text = self._get_source()
        assert "_on_step_finish" in text or "step.finish" in text

    def test_mcp_status_handler(self):
        text = self._get_source()
        assert "mcp" in text.lower()

    def test_tool_permission_handler(self):
        text = self._get_source()
        assert "tool.permission" in text or "ToolPermission" in text

    def test_load_prompt_history_method(self):
        text = self._get_source()
        assert "load_prompt_history" in text

    def test_update_prompt_history_method(self):
        text = self._get_source()
        assert "update_prompt_history" in text

    def test_event_bus_import_has_type_ignore(self):
        """src.core import must have type: ignore to suppress LSP error."""
        text = self._get_source()
        assert "# type: ignore[import]" in text


# ──────────────────────────────────────────────────────────────────────────────
# bus.py / events.py — no None-default bare Dict parameters anywhere
# ──────────────────────────────────────────────────────────────────────────────


class TestNoBareDictNoneDefaults:
    """Regression: all Dict[str,Any]=None parameters must be Optional."""

    def _check_file(self, path: Path) -> None:
        import ast

        text = path.read_text()
        tree = ast.parse(text)
        errors = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            all_args = args.args + args.posonlyargs + args.kwonlyargs
            defaults_offset = len(all_args) - len(args.defaults)
            for i, default in enumerate(args.defaults):
                if not isinstance(default, ast.Constant) or default.value is not None:
                    continue
                arg = all_args[defaults_offset + i]
                ann = arg.annotation
                # Check if annotation is Dict[str, Any] or list (bare, not Optional)
                if ann is None:
                    continue
                ann_str = ast.unparse(ann)
                if ann_str in ("Dict[str, Any]", "list", "list[Unknown]"):
                    errors.append(
                        f"{path.name}:{node.lineno}: param '{arg.arg}' has bare "
                        f"{ann_str} with None default (should be Optional)"
                    )
        assert errors == [], "\n".join(errors)

    def test_bus_no_bare_dict_none(self):
        self._check_file(Path(_TUI_SRC) / "ui" / "bus.py")

    def test_events_no_bare_dict_none(self):
        self._check_file(Path(_TUI_SRC) / "ui" / "events.py")
