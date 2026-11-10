from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from tui.tui_src.ui.features.palette.logic import (
    COMMAND_LIST,
    get_all_commands,
    filter_commands,
)
from tui.tui_src.ui.events import PaletteCommand, SlashCommand
from tui.tui_src.ui.app import AgentApp


def test_palette_command_list_includes_slash_commands() -> None:
    actions = {item["action"] for item in COMMAND_LIST}
    expected_actions = {
        "slash:diff",
        "slash:worktree",
        "slash:timeline",
        "slash:mcp",
        "slash:fast",
        "slash:undo",
        "slash:export",
        "action_open_sessions",
        "action_open_editor",
    }
    assert expected_actions.issubset(actions), f"Missing actions: {expected_actions - actions}"


def test_palette_filtering() -> None:
    # Filter by slash command keyword
    diff_results = filter_commands("diff")
    assert any(cmd["action"] == "slash:diff" for cmd in diff_results)

    # Filter by category
    git_results = filter_commands("git")
    assert any(cmd["action"] == "slash:diff" for cmd in git_results)
    assert any(cmd["action"] == "slash:worktree" for cmd in git_results)

    # Filter by sessions
    session_results = filter_commands("sessions browser")
    assert any(cmd["action"] == "action_open_sessions" for cmd in session_results)


def test_palette_slash_query_filtering() -> None:
    # Filter by slash string with leading slash
    status_results = filter_commands("/status")
    assert any(cmd["action"] == "slash:status" for cmd in status_results)

    doctor_results = filter_commands("/doctor")
    assert any(cmd["action"] == "slash:doctor" for cmd in doctor_results)

    clear_results = filter_commands("/clear")
    assert any(cmd["action"] == "slash:clear" for cmd in clear_results)

    fork_results = filter_commands("/fork")
    assert any(cmd["action"] == "slash:fork" for cmd in fork_results)


@pytest.mark.asyncio
async def test_command_item_renders_slash_badge() -> None:
    from tui.tui_src.ui.features.palette.screen import CommandItem

    test_app = AgentApp()
    async with test_app.run_test():
        item = CommandItem(label="Status", hint="Check status", action="slash:status", slash="/status")
        assert item.command_slash == "/status"
        composed = list(item.compose())
        assert len(composed) == 1
        rendered_text = str(composed[0].render())
        assert "/status" in rendered_text
        assert "Status" in rendered_text


def test_handle_palette_command_slash_dispatch() -> None:
    app = AgentApp()
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


def test_handle_palette_command_action_dispatch() -> None:
    app = AgentApp()
    app.action_open_sessions = MagicMock()
    app.action_open_editor = MagicMock()

    app.handle_palette_command(PaletteCommand("action_open_sessions"))
    app.action_open_sessions.assert_called_once()

    app.handle_palette_command(PaletteCommand("action_open_editor"))
    app.action_open_editor.assert_called_once()


@pytest.mark.asyncio
async def test_slash_export_alias() -> None:
    app = AgentApp()
    app._slash_share = AsyncMock()

    # Test /export triggers _slash_share
    event = MagicMock()
    event.command = "export"
    event.args = ""
    await app.handle_slash_command(event)
    app._slash_share.assert_awaited_once()
