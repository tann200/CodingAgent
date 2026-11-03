import pytest
from unittest.mock import MagicMock
from tui.tui_src.ui.app import AgentApp
from textual.widgets import Static


def test_app_watch_context_window_updates_sb_tokens():
    # Test watcher logic directly without running full Textual driver
    app = AgentApp()
    app.total_tokens = 4000
    app.context_window = 32000

    mock_sb_tokens = MagicMock()
    app.query_one = MagicMock(return_value=mock_sb_tokens)
    app._update_status_bar = MagicMock()

    # Change context window to 128k (e.g. Claude / GPT-4o / Gemini)
    app.watch_context_window(128000)

    mock_sb_tokens.update.assert_called_once()
    rendered = mock_sb_tokens.update.call_args[0][0]
    assert "4,000 / 128,000" in rendered
    assert "(3.1%)" in rendered
    app._update_status_bar.assert_called_once()


def test_app_watch_total_tokens_updates_sb_tokens():
    app = AgentApp()
    app.total_tokens = 0
    app.context_window = 64000

    mock_sb_tokens = MagicMock()
    app.query_one = MagicMock(return_value=mock_sb_tokens)
    app._update_status_bar = MagicMock()

    # Update total tokens
    app.watch_total_tokens(16000)

    mock_sb_tokens.update.assert_called_once()
    rendered = mock_sb_tokens.update.call_args[0][0]
    assert "16,000 / 64,000" in rendered
    assert "(25.0%)" in rendered
    app._update_status_bar.assert_called_once()
