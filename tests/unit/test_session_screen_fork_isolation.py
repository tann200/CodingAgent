import json
import uuid
from pathlib import Path
import pytest
from textual.app import App, ComposeResult
from tui.tui_src.ui.screens.session_screen import SessionScreen


@pytest.mark.asyncio
async def test_fork_session_assigns_new_unique_session_id(tmp_path: Path):
    ancestor_id = str(uuid.uuid4())
    ancestor_session_data = {
        "session_id": ancestor_id,
        "task_name": "original task",
        "working_dir": str(tmp_path),
        "messages": [{"role": "user", "content": "hello"}],
    }
    ancestor_path = tmp_path / f"session_{ancestor_id}.json"
    ancestor_path.write_text(json.dumps(ancestor_session_data), encoding="utf-8")

    class TestApp(App):
        def _get_sessions_dir(self):
            return tmp_path

    app = TestApp()
    async with app.run_test():
        screen = SessionScreen()
        await app.push_screen(screen)
        screen._show_new_session = False
        screen._filtered = [(ancestor_path, ancestor_session_data)]
        screen._selected = 0

        screen.action_fork_session()

        forked_files = list(tmp_path.glob("session_fork_*.json"))
        assert len(forked_files) == 1
        forked_data = json.loads(forked_files[0].read_text(encoding="utf-8"))

        # The forked session must NOT share the ancestor's session_id
        assert forked_data["session_id"] != ancestor_id
        # The forked session must have a valid UUID
        parsed_uuid = uuid.UUID(forked_data["session_id"])
        assert str(parsed_uuid) == forked_data["session_id"]
        assert forked_data["forked_from"] == ancestor_path.name
        assert forked_data["task_name"] == "[fork] original task"
