import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from tui.tui_src.ui._app_slash_commands_mixin import AppSlashCommandsMixin
from tui.tui_src.ui._app_session_mixin import AppSessionMixin
from tui.tui_src.ui.events import SlashCommand


class DummyApp(AppSlashCommandsMixin, AppSessionMixin):
    def __init__(self, sessions_dir: Path):
        self._sessions_dir = sessions_dir
        self._session_id = "test-session-uuid-123"
        self._session_custom_title = None
        self.sub_title = ""
        self.agent_running = False
        self._mounted_widgets = []
        self._bridge = MagicMock()
        self._bridge.history = [
            ("user", "first original message"),
            ("assistant", "hello world"),
            ("user", "second message"),
        ]
        self._bridge.get_turn_count.return_value = 2
        self._bridge.get_usage_totals.return_value = (100, 200)
        self._bridge.working_dir = str(sessions_dir)
        self._bridge._history_lock = MagicMock()
        self._bridge._history_lock.__enter__ = MagicMock()
        self._bridge._history_lock.__exit__ = MagicMock()

    def _get_sessions_dir(self) -> Path:
        return self._sessions_dir

    async def _mount_chat_widget(self, w):
        self._mounted_widgets.append(w)


@pytest.mark.asyncio
async def test_session_title_persists_across_snapshots(tmp_path: Path):
    app = DummyApp(tmp_path)

    # Initial snapshot before rename
    app._save_session_snapshot()
    snap_path = tmp_path / f"session_{app._session_id}.json"
    assert snap_path.exists()
    data = json.loads(snap_path.read_text(encoding="utf-8"))
    assert data["task_name"] == "first original message"

    # Execute /rename
    ev = SlashCommand(command="rename", args="Feature: Multi-File Refactor")
    await app.handle_slash_command(ev)

    assert app._session_custom_title == "Feature: Multi-File Refactor"
    assert app.sub_title == "Feature: Multi-File Refactor"

    # Inspect snapshot immediately after /rename
    data = json.loads(snap_path.read_text(encoding="utf-8"))
    assert data["task_name"] == "Feature: Multi-File Refactor"

    # Subsequent turn takes another snapshot; custom title must NOT be overwritten by first_user
    app._bridge.history.append(("user", "third message"))
    app._save_session_snapshot()

    data = json.loads(snap_path.read_text(encoding="utf-8"))
    assert data["task_name"] == "Feature: Multi-File Refactor"
