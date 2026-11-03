import json
import pytest
from unittest.mock import MagicMock
from tui.tui_src.ui.screens.session_list import SessionListScreen


@pytest.mark.asyncio
async def test_session_list_screen_async_loading(tmp_path):
    # Create test session files
    s1 = tmp_path / "session_20261103_01.json"
    s1.write_text(json.dumps({
        "session_id": "sess-1",
        "task_name": "Fix TypeScript bug",
        "timestamp": 1730678400,
        "message_count": 5,
        "role": "lead_architect",
    }), encoding="utf-8")

    s2 = tmp_path / "session_20261103_02.json"
    s2.write_text(json.dumps({
        "session_id": "sess-sub",
        "task_name": "Subagent research",
        "timestamp": 1730678500,
        "message_count": 2,
        "parent_session_id": "sess-1",
        "role": "researcher",
    }), encoding="utf-8")

    screen = SessionListScreen.__new__(SessionListScreen)
    mock_app = MagicMock()
    mock_app._get_sessions_dir = MagicMock(return_value=tmp_path)
    screen._app = mock_app
    screen._filter_subagents = False
    screen._all_sessions = []
    screen._filtered = []

    # Test async loading
    await screen._load_sessions_async()

    assert len(screen._all_sessions) == 2
    assert len(screen._filtered) == 2

    # Test with subagent filter
    screen._filter_subagents = True
    await screen._load_sessions_async()

    assert len(screen._all_sessions) == 1
    assert screen._all_sessions[0][1]["session_id"] == "sess-sub"
