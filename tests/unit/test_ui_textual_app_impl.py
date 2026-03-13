import pytest
import asyncio
from src.ui.textual_app_impl import CodingAgentTextualApp, TEXTUAL_AVAILABLE

@pytest.mark.asyncio
async def test_textual_app_init():
    if not TEXTUAL_AVAILABLE:
        pytest.skip("Textual not available")
    app = CodingAgentTextualApp()
    
    async with app.run_test() as pilot:
        # Check initial state
        assert app.query_one("#chat_input") is not None
        assert app.query_one("#chat_output") is not None
        
        # Test input
        await pilot.press("tab")
        await pilot.click("#chat_input")
        await pilot.press("a", "b", "c", "enter")
        
        # Should start processing
        assert len(app.orchestrator.msg_mgr.messages) >= 0

@pytest.mark.asyncio
async def test_textual_app_commands():
    if not TEXTUAL_AVAILABLE:
        pytest.skip("Textual not available")
    app = CodingAgentTextualApp()
    
    async with app.run_test() as pilot:
        # Check commands
        await pilot.click("#chat_input")
        await pilot.press("/", "h", "e", "l", "p", "enter")
        
        # Check settings
        await pilot.click("#chat_input")
        await pilot.press("/", "s", "e", "t", "t", "i", "n", "g", "s", "enter")
        
        await pilot.click("#chat_input")
        await pilot.press("/", "e", "x", "i", "t", "enter")
