import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tui.tui_src.ui._app_slash_commands_mixin import AppSlashCommandsMixin


class DummyWidget:
    def __init__(self):
        self.value = None

    def update(self, val):
        self.value = val


class DummyApp(AppSlashCommandsMixin):
    def __init__(self, initial_workdir: Path):
        self._bridge = MagicMock()
        self._bridge.working_dir = initial_workdir
        self.orch = MagicMock()
        self.orch.working_dir = str(initial_workdir)
        self._bridge._orchestrator = self.orch
        self._mounted_widgets = []
        self._notifications = []
        self._widgets = {
            "#sb_workdir": DummyWidget(),
            "#sb_git": DummyWidget(),
        }

    async def _mount_chat_widget(self, w):
        self._mounted_widgets.append(w)

    def notify(self, msg, severity="info"):
        self._notifications.append((msg, severity))

    def query_one(self, selector, expected_type=None):
        if selector in self._widgets:
            return self._widgets[selector]
        raise KeyError(selector)


@pytest.mark.asyncio
async def test_slash_worktree_create_and_remove_synchronization(tmp_path):
    orig_dir = tmp_path / "main_repo"
    orig_dir.mkdir()
    wt_dir = tmp_path / "agent-wt-test"
    wt_dir.mkdir()

    app = DummyApp(orig_dir)
    mock_mgr = MagicMock()
    mock_mgr.create = AsyncMock(return_value=wt_dir)
    mock_mgr.remove = AsyncMock(return_value=True)
    mock_mgr.active = {"task-1": wt_dir}
    app._worktree_mgr = mock_mgr

    # 1. Create worktree
    await app._slash_worktree("create task-1")

    assert app._bridge.working_dir == wt_dir
    assert app.orch.working_dir == str(wt_dir)
    assert app._widgets["#sb_workdir"].value == str(wt_dir)
    assert app.orch._publish_git_status.call_count >= 1
    assert len(app._mounted_widgets) == 1
    assert "Worktree created & switched" in str(app._mounted_widgets[0].render())

    app.orch._publish_git_status.reset_mock()

    # 2. Remove worktree
    await app._slash_worktree("remove task-1")

    # Must switch back to original directory
    assert app._bridge.working_dir == orig_dir
    assert app.orch.working_dir == str(orig_dir)
    assert app._widgets["#sb_workdir"].value == str(orig_dir)
    assert app.orch._publish_git_status.call_count >= 1
    assert len(app._mounted_widgets) == 2
    assert "switched back" in str(app._mounted_widgets[1].render())
