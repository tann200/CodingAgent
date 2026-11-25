"""Unit & Textual pilot tests for TASK-37: critical TUI bugfixes & terminal security."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tui.tui_src.ui.components.bash_block import BashBlock
from tui.tui_src.ui.components.chat_input import ChatTextArea
from tui.tui_src.ui.components.chat_mixin import ChatDisplayMixin, _MAX_CHAT_WIDGETS
from tui.tui_src.ui.components.stream_view import StreamView
from tui.tui_src.ui.components.thinking import ThinkingProcess
from tui.tui_src.ui.terminal_sanitize import strip_terminal_controls


class TestTerminalSanitizer:
    def test_strips_csi_osc_dcs_and_c1_controls(self) -> None:
        raw = "\x1b[31mred\x1b[0m \x1b]0;spoofed-title\x07\x1bPignored\x1b\\ok\x9b2J\tkeep\nline2"
        cleaned = strip_terminal_controls(raw)
        assert cleaned == "red ok2J\tkeep\nline2"
        assert "\x1b" not in cleaned
        assert "\x07" not in cleaned
        assert "\x9b" not in cleaned


class TestPruneChatLogAndAtFileSecurity:
    def test_prune_chat_log_bounded_when_remove_is_async(self) -> None:
        removed: list[int] = []

        class _FakeChild:
            def __init__(self, idx: int) -> None:
                self.idx = idx

            def remove(self) -> None:
                # Simulates Textual async remove(): does NOT mutate parent.children synchronously
                removed.append(self.idx)

        class _FakeContainer:
            def __init__(self, count: int) -> None:
                self.children = [_FakeChild(i) for i in range(count)]

        class _Host(ChatDisplayMixin):
            def __init__(self) -> None:
                self._c = _FakeContainer(_MAX_CHAT_WIDGETS + 5)

            def query_one(self, selector: str, *args: object) -> object:
                return self._c

        host = _Host()
        host._prune_chat_log()  # Must terminate immediately without infinite loop!
        assert removed == [0, 1, 2, 3, 4]

    def test_expand_at_tokens_blocks_sibling_prefix_and_secret_files(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        sibling = tmp_path / "repo-secrets"
        workspace.mkdir()
        sibling.mkdir()
        (workspace / "hello.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / ".env").write_text("API_KEY=supersecret\n", encoding="utf-8")
        (workspace / ".git").mkdir()
        (workspace / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (sibling / "leak.txt").write_text("SIBLING_SECRET", encoding="utf-8")

        notified: list[str] = []

        class _Host(ChatDisplayMixin):
            def __init__(self) -> None:
                self._bridge = MagicMock(working_dir=str(workspace))

            def notify(self, msg: str, **kwargs: object) -> None:
                notified.append(msg)

        host = _Host()
        # Allowed normal workspace file
        ok_out = host._expand_at_tokens("Check @hello.py")
        assert "print('ok')" in ok_out

        # Sibling-directory prefix bypass blocked
        sib_out = host._expand_at_tokens("Check @../repo-secrets/leak.txt")
        assert "SIBLING_SECRET" not in sib_out
        assert "@../repo-secrets/leak.txt" in sib_out

        # Secret .env blocked
        env_out = host._expand_at_tokens("Read @.env")
        assert "supersecret" not in env_out
        assert any(".env" in n for n in notified)

        # .git/config blocked
        git_out = host._expand_at_tokens("Read @.git/config")
        assert "[core]" not in git_out


class TestWidgetFixes:
    def test_stream_view_pre_mount_append_and_sanitization(self) -> None:
        sv = StreamView(role="Agent")
        # Not mounted yet — must NOT raise NoActiveAppError
        sv.append_chunk("\x1b]0;evil\x07hello ")
        sv.append_chunk("\x1b[32mworld\x1b[0m")
        assert sv.finalize() == "hello world"

    def test_chat_text_area_passes_placeholder(self) -> None:
        ta = ChatTextArea(placeholder="Type here…")
        assert ta.placeholder == "Type here…"

    def test_bash_block_sanitizes_and_has_vertical_auto_css(self) -> None:
        assert "layout: vertical;" in BashBlock.DEFAULT_CSS
        assert "height: auto;" in BashBlock.DEFAULT_CSS
        bb = BashBlock(description="test", command="pytest")
        bb.append_output("\x1b[31mFAIL\x1b[0m line")
        assert bb._output_lines == ["FAIL line"]


class TestQueueAndPilotFixes:
    def test_drain_message_queue_dispatches_one_at_a_time(self) -> None:
        from tui.tui_src.ui._app_message_handlers_mixin import AppMessageHandlersMixin

        sent: list[str] = []

        class _Host(AppMessageHandlersMixin):
            def __init__(self) -> None:
                self._queued_messages = deque(["msg1", "msg2", "msg3"])
                self.agent_running = False
                self._bridge = MagicMock()
                self._bridge.send_prompt.side_effect = lambda m: sent.append(m)

        host = _Host()
        host._drain_message_queue()
        assert sent == ["msg1"]
        assert list(host._queued_messages) == ["msg2", "msg3"]
        assert host.agent_running is True

    @pytest.mark.asyncio
    async def test_pilot_bare_slash_command_submits_on_enter_and_thinking_toggles_brackets(self) -> None:
        from textual.app import App, ComposeResult

        submitted: list[str] = []

        class _MiniApp(App[None], ChatDisplayMixin):
            def __init__(self) -> None:
                super().__init__()
                self._palette_active = False
                self._palette_matches: list[str] = []
                self._palette_index = 0
                self._at_picker_active = False
                self._at_picker_matches: list[str] = []
                self._at_picker_index = 0
                self._at_picker_widget = None
                self._at_prefix = ""
                self._bridge = MagicMock(working_dir=".")

            def compose(self) -> ComposeResult:
                yield ChatTextArea(id="user_input", placeholder="Ask…")
                yield ThinkingProcess("Thinking: check list[int] and [bold] tags", start_time=0.0)

            def on_chat_text_area_text_changed(self, event: ChatTextArea.TextChanged) -> None:
                self._chat_handle_text_changed(event)

            def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
                submitted.append(event.text)

        app = _MiniApp()
        async with app.run_test() as pilot:
            ta = app.query_one("#user_input", ChatTextArea)
            ta.focus()
            ta.load_text("/help")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert submitted == ["/help"]

            # Toggle ThinkingProcess containing [bold] & list[int] brackets without MarkupError
            tp = app.query_one(ThinkingProcess)
            tp._toggle_expansion()
            assert tp.is_expanded is True
            tp._toggle_expansion()
            assert tp.is_expanded is False
