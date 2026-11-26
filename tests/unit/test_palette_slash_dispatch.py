from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from tui.tui_src.ui.palette_provider import CodingAgentCommands, STATIC_PALETTE_COMMANDS
from tui.tui_src.ui.events import PaletteCommand, SlashCommand
from tui.tui_src.ui.app import AgentApp


def test_palette_command_list_includes_slash_commands() -> None:
    actions = {action for _, action, _ in STATIC_PALETTE_COMMANDS}
    expected_actions = {
        "slash:diff",
        "slash:worktree",
        "slash:timeline",
        "slash:mcp",
        "slash:fast",
        "slash:undo",
        "slash:export",
        "slash:theme",
        "action_open_sessions",
        "action_open_editor",
    }
    assert expected_actions.issubset(actions), f"Missing actions: {expected_actions - actions}"


@pytest.mark.asyncio
async def test_palette_filtering() -> None:
    app = AgentApp()
    async with app.run_test(size=(120, 40)):
        provider = CodingAgentCommands(app.screen)
        diff_hits = [h async for h in provider.search("diff")]
        assert any("diff" in str(h.match_display).lower() for h in diff_hits)

        session_hits = [h async for h in provider.search("sessions")]
        assert any("session" in str(h.match_display).lower() for h in session_hits)


@pytest.mark.asyncio
async def test_palette_slash_query_filtering() -> None:
    app = AgentApp()
    async with app.run_test(size=(120, 40)):
        provider = CodingAgentCommands(app.screen)
        for q in ("/status", "/doctor", "/clear", "/fork"):
            hits = [h async for h in provider.search(q)]
            assert hits, f"Expected native palette hit for {q}"


@pytest.mark.asyncio
async def test_command_item_renders_slash_badge() -> None:
    # Native Textual CommandPalette renders slash badges in Hit.match_display
    app = AgentApp()
    async with app.run_test(size=(120, 40)):
        provider = CodingAgentCommands(app.screen)
        hits = [h async for h in provider.search("/status")]
        assert any("/status" in str(h.match_display) for h in hits)


def test_handle_palette_command_slash_dispatch() -> None:
    app = AgentApp()
    try:
        posted_messages = []
        app.post_message = MagicMock(side_effect=lambda msg: posted_messages.append(msg))

        # Dispatch a slash command from palette
        app.handle_palette_command(PaletteCommand("slash:diff"))
        assert len(posted_messages) == 1
        assert isinstance(posted_messages[0], SlashCommand)
        assert posted_messages[0].command == "diff"
        assert posted_messages[0].args == ""

        # Dispatch slash command with args
        app.handle_palette_command(PaletteCommand("slash:worktree list"))
        assert len(posted_messages) == 2
        assert isinstance(posted_messages[1], SlashCommand)
        assert posted_messages[1].command == "worktree"
        assert posted_messages[1].args == "list"
    finally:
        app._bridge.cleanup()


def test_handle_palette_command_action_dispatch() -> None:
    app = AgentApp()
    try:
        app.action_open_sessions = MagicMock()
        app.action_open_editor = MagicMock()

        app.handle_palette_command(PaletteCommand("action_open_sessions"))
        app.action_open_sessions.assert_called_once()

        app.handle_palette_command(PaletteCommand("action_open_editor"))
        app.action_open_editor.assert_called_once()
    finally:
        app._bridge.cleanup()


@pytest.mark.asyncio
async def test_slash_export_alias() -> None:
    app = AgentApp()
    try:
        app._slash_share = AsyncMock()

        # Test /export triggers _slash_share
        event = MagicMock()
        event.command = "export"
        event.args = ""
        await app.handle_slash_command(event)
        app._slash_share.assert_awaited_once()
    finally:
        app._bridge.cleanup()
