"""TASK-38 tests: Textual 8.1.0 TUI modernization (MarkdownStream, themes, responsive layout, Collapsible, native CommandPalette, clean bridge boundary)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible

from tui.tui_src.ui.app import AgentApp
from tui.tui_src.ui.bus import AgentFinalResponse, StreamChunkEvent
from tui.tui_src.ui.components.artifact import AgentArtifact
from tui.tui_src.ui.components.bash_block import BashBlock
from tui.tui_src.ui.components.stream_view import StreamView
from tui.tui_src.ui.components.thinking import ThinkingProcess
from tui.tui_src.ui.events import SlashCommand
from tui.tui_src.ui.palette_provider import CodingAgentCommands

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_markdown_stream_in_place_finalization_no_duplicate_widgets() -> None:
    app = AgentApp()
    async with app.run_test(size=(120, 35)) as pilot:
        app.handle_stream_chunk(StreamChunkEvent(chunk="## Hello\n\n", is_partial=True))
        await pilot.pause(0.05)
        app.handle_stream_chunk(StreamChunkEvent(chunk="World **bold**!", is_partial=True))
        await pilot.pause(0.05)

        await app.handle_final_response(AgentFinalResponse(content="## Hello\n\nWorld **bold**!"))
        await pilot.pause(0.05)

        chat_log = app.query_one("#chat_log", VerticalScroll)
        stream_views = list(chat_log.query(StreamView))
        artifacts = list(chat_log.query(AgentArtifact))
        # Must stay mounted in-place as a single StreamView with zero duplicate AgentArtifact
        assert len(stream_views) == 1
        assert len(artifacts) == 0
        assert "World **bold**!" in stream_views[0].finalize()


@pytest.mark.asyncio
async def test_smart_auto_scroll_does_not_hijack_scrolled_up_viewport() -> None:
    app = AgentApp()
    async with app.run_test(size=(120, 20)) as pilot:
        chat_log = app.query_one("#chat_log", VerticalScroll)
        # Mount enough widgets to make chat_log scrollable
        from textual.widgets import Static

        for i in range(30):
            await chat_log.mount(Static(f"Line {i}\nDetail {i}", markup=False))
        await pilot.pause(0.05)
        assert chat_log.max_scroll_y > 5

        # Scroll up to top
        chat_log.scroll_to(y=0, animate=False)
        await pilot.pause(0.05)
        assert not app._should_auto_scroll(chat_log)

        # Mounting a new widget while scrolled up must NOT yank scroll_y back to bottom
        await app._mount_chat_widget(Static("New incoming line", markup=False))
        await pilot.pause(0.05)
        assert chat_log.scroll_y < chat_log.max_scroll_y
        jump_btn = app.query_one("#jump_to_latest_btn", Button)
        assert "visible" in jump_btn.classes


@pytest.mark.asyncio
async def test_theme_slash_command_switches_themes_and_tcss_uses_semantic_tokens(tmp_path: Path) -> None:
    tcss = (_REPO_ROOT / "tui" / "tui_src" / "ui" / "styles" / "app.tcss").read_text(encoding="utf-8")
    for legacy_hex in ("#161616", "#111111", "#262626", "#1a1a1a"):
        assert legacy_hex not in tcss, f"Hardcoded dark hex {legacy_hex} still present in app.tcss"
    assert "$background" in tcss and "$surface" in tcss and "$text" in tcss

    from tui.tui_src.ui.settings import SettingsStore

    app = AgentApp()
    app._settings = SettingsStore(path=tmp_path / "settings.json")
    async with app.run_test(size=(120, 35)) as pilot:
        await app.handle_slash_command(SlashCommand("theme", "textual-light"))
        await pilot.pause(0.05)
        assert app.theme == "textual-light"

        await app.handle_slash_command(SlashCommand("theme", "nord"))
        await pilot.pause(0.05)
        assert app.theme == "nord"


@pytest.mark.asyncio
async def test_narrow_terminal_auto_hides_sidebar_and_ctrl_b_toggles() -> None:
    app_narrow = AgentApp()
    async with app_narrow.run_test(size=(90, 30)) as pilot:
        await pilot.pause(0.05)
        sidebar = app_narrow.query_one("#right_sidebar", VerticalScroll)
        assert "hidden" in sidebar.classes

        # Manual toggle brings it back
        app_narrow.action_toggle_sidebar()
        await pilot.pause(0.05)
        assert "hidden" not in sidebar.classes

    app_wide = AgentApp()
    async with app_wide.run_test(size=(125, 30)) as pilot:
        await pilot.pause(0.05)
        sidebar = app_wide.query_one("#right_sidebar", VerticalScroll)
        assert "hidden" not in sidebar.classes


@pytest.mark.asyncio
async def test_collapsible_thinking_and_bash_block_widgets() -> None:
    app = AgentApp()
    async with app.run_test(size=(120, 35)) as pilot:
        chat_log = app.query_one("#chat_log", VerticalScroll)
        tp = ThinkingProcess("Step 1: check [bold]brackets[/bold] safely")
        bb = BashBlock(description="Run pytest", command="pytest -q")
        await chat_log.mount(tp)
        await chat_log.mount(bb)
        await pilot.pause()
        await pilot.pause(0.1)

        assert tp.query_one(Collapsible).collapsed is True
        tp._toggle_expansion()
        await pilot.pause(0.05)
        assert tp.is_expanded is True
        assert tp.query_one(Collapsible).collapsed is False

        bb.set_output([f"line {i}" for i in range(55)])
        await pilot.pause(0.05)
        assert bb.is_truncated is True
        bb.collapsed = False
        await pilot.pause(0.05)
        assert bb.is_truncated is False


@pytest.mark.asyncio
async def test_native_command_palette_provider_search_hits() -> None:
    app = AgentApp()
    assert app.ENABLE_COMMAND_PALETTE is True
    async with app.run_test(size=(120, 35)):
        provider = CodingAgentCommands(app.screen)
        hits_theme = [h async for h in provider.search("nord")]
        assert any("nord" in str(h.match_display).lower() for h in hits_theme)

        hits_skill = [h async for h in provider.search("code_review")]
        assert any("code_review" in str(h.match_display).lower() for h in hits_skill)


def test_app_py_has_zero_src_core_imports_and_frees_tab_binding() -> None:
    app_src = (_REPO_ROOT / "tui" / "tui_src" / "ui" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(app_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("src.core"), f"Direct src.core import in app.py: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("src.core"), f"Direct src.core import in app.py: {mod}"
    assert "spec_from_file_location" not in app_src

    binding_keys = {b[0] if isinstance(b, tuple) else getattr(b, "key", "") for b in AgentApp.BINDINGS}
    assert "tab" not in binding_keys
    assert "shift+tab" in binding_keys
    assert "ctrl+b" in binding_keys
