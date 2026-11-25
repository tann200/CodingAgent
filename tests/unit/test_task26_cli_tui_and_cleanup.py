"""TASK-26 regression tests: dead subsystem removal + CLI/TUI drift fixes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dead_subsystems_and_tui_stubs_removed() -> None:
    retired = [
        "src/core/orchestration/file_lock_manager.py",
        "src/core/orchestration/session_registry.py",
        "src/core/orchestration/cross_session_bus.py",
        "tui/tui_src/ui/commands/registry.py",
        "tui/tui_src/ui/components/inline_tool.py",
        "tui/tui_src/ui/screens/probe_results.py",
        "tui/tui_src/ui/main.py",
        "tui/tui_src/ui/mock_engine.py",
    ]
    for rel in retired:
        assert not (REPO_ROOT / rel).exists(), f"Expected retired module to be deleted: {rel}"


def test_status_cmd_reads_workspace_settings_and_sqlite_sessions(tmp_path: Path, capsys) -> None:
    from src.cli.status_cmd import run_status
    from src.core.memory.session_store import SessionStore

    cfg_dir = tmp_path / ".codingAgent"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.json").write_text(
        json.dumps({"default_provider": "ollama", "default_model": "qwen3.5:9b"}),
        encoding="utf-8",
    )
    store = SessionStore(workdir=str(tmp_path))
    try:
        store.add_message("s-status-1", "user", "hello")
    finally:
        store.close()

    rc = run_status(SimpleNamespace(workdir=str(tmp_path), json=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provider"] == "ollama"
    assert out["model"] == "qwen3.5:9b"
    assert out["sessions_available"] == 1


def test_mcp_cmd_preserves_command_and_url_fallbacks(tmp_path: Path) -> None:
    from src.cli.mcp_cmd import _configured_servers

    fake_servers = [
        {"name": "stdio-cmd", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-fs"]},
        {"name": "legacy-cmd", "cmd": ["python3", "srv.py"]},
        {"name": "remote-http", "transport": "http", "url": "https://example.com/mcp"},
    ]
    with patch("src.core.config_loader.get_mcp_servers", return_value=fake_servers):
        servers = _configured_servers(str(tmp_path))

    by_name = {s["name"]: s for s in servers}
    assert by_name["stdio-cmd"]["cmd"] == "npx -y @modelcontextprotocol/server-fs"
    assert by_name["legacy-cmd"]["cmd"] == "python3 srv.py"
    assert by_name["remote-http"]["cmd"] == "https://example.com/mcp"


def test_diff_cmd_handles_unborn_head_staged_and_untracked(tmp_path: Path, capsys) -> None:
    from src.cli.diff_cmd import run_diff

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    staged = tmp_path / "staged.py"
    staged.write_text("print('staged')\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.py"], cwd=tmp_path, check=True)
    untracked = tmp_path / "new_file.txt"
    untracked.write_text("hello\n", encoding="utf-8")

    rc = run_diff(SimpleNamespace(workdir=str(tmp_path), json=True, path=None))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base"] == "unborn HEAD"
    assert "staged.py" in payload["diff"]
    assert "new_file.txt" in payload["diff"]
    assert "(no changes" not in payload["diff"]


def test_chat_input_slash_commands_double_esc_and_descending_frecency() -> None:
    from tui.tui_src.ui.components.chat_input import (
        SLASH_COMMANDS,
        SLASH_COMMAND_DESCRIPTIONS,
        ChatTextArea,
    )

    for cmd in ("/doctor", "/skills", "/revert", "/share", "/export", "/rename", "/worktree"):
        assert cmd in SLASH_COMMANDS
        assert cmd in SLASH_COMMAND_DESCRIPTIONS

    # Verify descending frecency order (index 0 = most frequent/recent) & double-Esc guard
    area = ChatTextArea()
    area._prompt_history = ["most-frequent", "second", "oldest"]
    posted: list[object] = []
    area.post_message = posted.append  # type: ignore[method-assign]
    area.load_text = MagicMock()  # type: ignore[method-assign]
    area.move_cursor = MagicMock()  # type: ignore[method-assign]
    area.clear = MagicMock()  # type: ignore[method-assign]

    # Single Esc must NOT fire InterruptSignal; second Esc within 500ms DOES
    ev_esc = MagicMock(key="escape")
    with patch.object(ChatTextArea, "app", new=SimpleNamespace(_palette_active=False, _at_picker_active=False)):
        area.on_key(ev_esc)
        assert not any(type(m).__name__ == "InterruptSignal" for m in posted)
        area.on_key(ev_esc)
        assert any(type(m).__name__ == "InterruptSignal" for m in posted)

        # Up arrow walks descending frecency: index 0 ("most-frequent"), then index 1 ("second")
        ev_up = MagicMock(key="up")
        with patch.object(ChatTextArea, "cursor_at_first_line", new=True):
            area.on_key(ev_up)
            area.load_text.assert_called_with("most-frequent")
            area.on_key(ev_up)
            area.load_text.assert_called_with("second")
