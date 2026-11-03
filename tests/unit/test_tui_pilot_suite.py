import pytest
from tui.tui_src.ui.app import AgentApp
from textual.widgets import Static


@pytest.mark.asyncio
async def test_tui_headless_pilot_lifecycle():
    app = AgentApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # Check initial widgets
        assert app.query_one("#user_input") is not None
        assert app.query_one("#sb_tokens") is not None
        assert app.query_one("#chat_log") is not None
        assert app.query_one("#sb_role") is not None

        # Test role toggling
        initial_role = app.active_role
        app.action_toggle_mode()
        await pilot.pause(0.05)
        assert app.active_role != initial_role
        assert app.active_role == "full_stack_engineer"

        # Test toggle console panel
        console = app.query_one("#console_panel")
        initial_has_hidden = console.has_class("hidden")
        app.action_toggle_console()
        await pilot.pause(0.05)
        assert console.has_class("hidden") != initial_has_hidden

        # Toggle back
        app.action_toggle_console()
        await pilot.pause(0.05)
        assert console.has_class("hidden") == initial_has_hidden

        # Test opening settings screen and dismissing
        base_stack_len = len(app.screen_stack)
        app.action_open_settings()
        await pilot.pause(0.05)
        assert len(app.screen_stack) == base_stack_len + 1
        app.screen.dismiss()
        await pilot.pause(0.05)
        assert len(app.screen_stack) == base_stack_len

        # Test opening sessions screen and dismissing
        app.action_open_sessions()
        await pilot.pause(0.05)
        assert len(app.screen_stack) == base_stack_len + 1
        app.screen.dismiss()
        await pilot.pause(0.05)
        assert len(app.screen_stack) == base_stack_len
