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


@pytest.mark.asyncio
async def test_tui_headless_pilot_palette_and_tool_events():
    from tui.tui_src.ui.bus import ToolCallStartEvent, ToolCallFinishEvent
    from tui.tui_src.ui.features.palette.screen import CommandPalette

    app = AgentApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # Test opening command palette
        base_stack_len = len(app.screen_stack)
        app.action_open_palette()
        await pilot.pause(0.05)
        assert len(app.screen_stack) == base_stack_len + 1
        assert isinstance(app.screen, CommandPalette)

        # Type in palette input to filter commands by slash
        palette_input = app.screen.query_one("#palette_input")
        palette_input.value = "/clear"
        await pilot.pause(0.05)
        footer = app.screen.query_one("#palette_footer")
        assert "commands" in str(footer.render())

        # Dismiss palette
        app.screen.dismiss()
        await pilot.pause(0.05)
        assert len(app.screen_stack) == base_stack_len

        # Test tool call lifecycle with bracketed strings in output
        app.handle_tool_start(
            ToolCallStartEvent(
                tool_name="bash",
                tool_args={"command": "cat [slug]/page.tsx"},
                tool_id="call_123",
            )
        )
        await pilot.pause(0.05)

        app.handle_tool_finish(
            ToolCallFinishEvent(
                tool_name="bash",
                tool_id="call_123",
                result_text="[INFO] Successfully compiled [data]\n[1/10] Done",
                ok=True,
            )
        )
        await pilot.pause(0.05)

        # Non-bash tool call with brackets
        app.handle_tool_start(
            ToolCallStartEvent(
                tool_name="custom_tool",
                tool_args={"file": "[id].py"},
                tool_id="call_456",
            )
        )
        await pilot.pause(0.05)
        app.handle_tool_finish(
            ToolCallFinishEvent(
                tool_name="custom_tool",
                tool_id="call_456",
                result_text="[ERROR] Cannot process [id].py: [reason: not found]",
                ok=False,
            )
        )
        await pilot.pause(0.05)
        # Verify app didn't crash and chat log mounted widgets
        chat_log = app.query_one("#chat_log")
        assert len(chat_log.children) > 0


@pytest.mark.asyncio
async def test_tui_mixin_message_dispatch_and_cli_args(tmp_path):
    """Verify mixin @on handlers, SlashCommand, and CLI init args work via Textual dispatch."""
    from tui.tui_src.ui.bus import (
        StreamChunkEvent,
        AgentFinalResponse,
        StatusUpdate,
        ToolCallStartEvent,
    )
    from tui.tui_src.ui.events import SlashCommand

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    app = AgentApp(working_dir=tmp_path, trusted=False, plugin_dir=str(plugin_dir))

    # Constructor must wire CLI args BEFORE Orchestrator/Bridge creation
    assert app._bridge._working_dir == str(tmp_path)
    assert app._bridge._orchestrator.working_dir == tmp_path
    assert getattr(app._bridge._orchestrator, "trusted", None) is False
    assert getattr(app, "_initial_plugin_dir", None) == str(plugin_dir)

    async with app.run_test(size=(120, 40)) as pilot:
        chat_log = app.query_one("#chat_log")
        initial_children = len(chat_log.children)

        # Post SlashCommand via Textual message queue (not direct method call)
        app.post_message(SlashCommand("help"))
        await pilot.pause(0.1)
        assert len(chat_log.children) > initial_children
        assert any(w.has_class("help_msg") for w in chat_log.children)

        # Post StreamChunkEvent and AgentFinalResponse via Textual dispatch
        app.post_message(StreamChunkEvent(chunk="Streaming chunk...", is_partial=True))
        await pilot.pause(0.1)
        assert app._current_stream is not None or app._active_stream_view is not None

        app.post_message(AgentFinalResponse(content="Final answer"))
        await pilot.pause(0.1)

        # Post ToolCallStartEvent via Textual dispatch
        app.post_message(
            ToolCallStartEvent(
                tool_name="read_file",
                tool_args={"path": "README.md"},
                tool_id="dispatch_t1",
            )
        )
        await pilot.pause(0.1)
        assert "dispatch_t1" in app._tool_widgets

        # Post StatusUpdate via Textual dispatch
        app.post_message(StatusUpdate(message="thinking..."))
        await pilot.pause(0.05)
        assert "thinking..." in str(app.query_one("#sb_status", Static).render())
