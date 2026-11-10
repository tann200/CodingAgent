import pytest
from unittest.mock import MagicMock
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.widgets import Static

from tui.tui_src.ui.components.file_picker import FilePickerOverlay
from tui.tui_src.ui.components.diff_viewer import SideBySideDiff
from tui.tui_src.ui._app_tool_handlers_mixin import AppToolHandlersMixin
from tui.tui_src.ui.bus import ToolCallStartEvent, ToolCallErrorEvent, ToolExecutionNotice


def test_file_picker_overlay_escapes_bracket_paths():
    picker = FilePickerOverlay()
    bracket_paths = [
        "app/[id]/route.ts",
        "components/[slug]/page.tsx",
        "data/[key]/[val].json",
    ]
    picker.update_picker(bracket_paths, 0)
    rendered = str(picker.render())
    assert "[id]" in rendered
    assert "[slug]" in rendered


@pytest.mark.asyncio
async def test_side_by_side_diff_escapes_bracket_paths():
    class TestDiffApp(App):
        def compose(self) -> ComposeResult:
            diff_text = "--- a/old\n+++ b/new\n@@ -1 +1 @@\n-old\n+new\n"
            yield SideBySideDiff(path="app/[slug]/page.tsx", diff=diff_text)

    test_app = TestDiffApp()
    async with test_app.run_test():
        diff_widget = test_app.query_one(SideBySideDiff)
        header = diff_widget.query_one(".sbs_header", Static)
        rendered_header = str(header.render())
        assert "[slug]/page.tsx" in rendered_header


def test_tool_handlers_escape_bracket_commands():
    class DummyApp(AppToolHandlersMixin):
        def __init__(self):
            self._tool_call_count = 0
            self._tool_args = {}
            self._tool_widgets = {}
            self._scheduled_widgets = []
            self.query_one = MagicMock()

        def _sched_chat_widget(self, w):
            self._scheduled_widgets.append(w)

    app = DummyApp()

    ev = ToolCallStartEvent(
        tool_name="bash",
        tool_args={"command": 'echo "[test]" && pytest -k "[slug]"', "description": "Run tests [ci]"},
        tool_id="call-123",
    )
    app.handle_tool_start(ev)
    assert len(app._scheduled_widgets) == 1
    w = app._scheduled_widgets[0]
    rendered = str(w.render())
    assert "[slug]" in rendered
    assert "[ci]" in rendered


def test_tool_error_escapes_bracket_errors():
    class DummyApp(AppToolHandlersMixin):
        def __init__(self):
            self._tool_widgets = {}
            self._scheduled_widgets = []
            self.query_one = MagicMock()

        def _sched_chat_widget(self, w):
            self._scheduled_widgets.append(w)

    app = DummyApp()
    ev = ToolCallErrorEvent(
        tool_name="run_tests",
        error="IndexError: list index [0] out of range for pattern [a-z]*",
        tool_id="call-456",
    )
    app.handle_tool_error(ev)
    assert len(app._scheduled_widgets) == 1
    w = app._scheduled_widgets[0]
    rendered = str(w.render())
    assert "[0]" in rendered
    assert "[a-z]*" in rendered


def test_tool_notice_escapes_arguments():
    class DummyApp(AppToolHandlersMixin):
        def __init__(self):
            self._scheduled_widgets = []

        def _sched_chat_widget(self, w):
            self._scheduled_widgets.append(w)

    app = DummyApp()
    ev = ToolExecutionNotice(
        tool_name="edit_file",
        arguments={"path": "app/[id]/layout.tsx", "pattern": "items[0]"},
    )
    app.handle_tool_notice(ev)
    assert len(app._scheduled_widgets) == 1
    w = app._scheduled_widgets[0]
    rendered = str(w.render())
    assert "[id]" in rendered
    assert "items[0]" in rendered
