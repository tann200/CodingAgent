import pytest
from unittest.mock import MagicMock
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Static

from tui.tui_src.ui._app_message_handlers_mixin import AppMessageHandlersMixin
from tui.tui_src.ui.bus import ModelRoutingEvent


def test_model_routing_scales_context_window():
    class DummyApp(AppMessageHandlersMixin):
        def __init__(self):
            self.context_window = 32000
            self.total_tokens = 0
            self.query_one = MagicMock()

    app = DummyApp()

    # Route to a model with 1,000,000 token context
    ev = ModelRoutingEvent(
        provider="gemini",
        model="gemini-1.5-pro",
    )
    app.handle_model_routing(ev)

    # Context window should scale up from 32000 to the model profile max_context (>= 1,000,000)
    assert app.context_window >= 1_000_000


@pytest.mark.asyncio
async def test_watch_context_window_updates_sb_tokens_widget():
    from tui.tui_src.ui.app import AgentApp

    class SimpleTokenApp(App):
        context_window: reactive[int] = reactive(32000)
        total_tokens: reactive[int] = reactive(5000)

        def compose(self) -> ComposeResult:
            yield Static("0 / 32,000  (0.0%)", id="sb_tokens")

        def watch_context_window(self, new_limit: int) -> None:
            # Delegate to the exact logic from AgentApp
            AgentApp.watch_context_window(self, new_limit)

    test_app = SimpleTokenApp()
    async with test_app.run_test():
        # Change context window to 200,000
        test_app.context_window = 200000
        sb_tokens = test_app.query_one("#sb_tokens", Static)
        text = str(sb_tokens.render())
        assert "200,000" in text
        assert "5,000" in text
