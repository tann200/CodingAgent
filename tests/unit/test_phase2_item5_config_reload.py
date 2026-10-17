import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.config_hot_reload import ConfigReloader, get_config_reloader
import src.core.config_loader as config_loader
from src.core.orchestration.instruction_loader import load_project_instructions, _INSTRUCTION_FILENAMES


def test_config_reloader_no_infinite_recursion():
    # Calling get_config_reloader should not recursively call load_merged_config
    with patch("src.core.config_hot_reload._config_reloader", None):
        reloader = get_config_reloader(initial_load=False)
        assert reloader is not None
        assert reloader._initialized is True
        assert reloader._pending_reload is False
        assert reloader._is_loading is False
        reloader.stop()


def test_config_reloader_watches_active_config_hierarchy(tmp_path: Path):
    agent_dir = tmp_path / ".codingAgent"
    agent_dir.mkdir(parents=True)
    cfg_file = agent_dir / "config.json"
    cfg_file.write_text(json.dumps({"max_turns": 10}), encoding="utf-8")

    prov_file = agent_dir / "providers.json"
    prov_file.write_text(json.dumps([{"id": "p1"}]), encoding="utf-8")

    reloader = ConfigReloader(initial_load=False, workdir=tmp_path)
    watched = reloader._get_watched_paths()
    assert cfg_file in watched
    assert prov_file in watched
    reloader.stop()


def test_config_reloader_detects_changes(tmp_path: Path):
    agent_dir = tmp_path / ".codingAgent"
    agent_dir.mkdir(parents=True)
    cfg_file = agent_dir / "config.json"
    cfg_file.write_text(json.dumps({"max_turns": 10}), encoding="utf-8")

    reloader = ConfigReloader(initial_load=False, workdir=tmp_path)
    assert reloader.changed() is False

    # Modify file
    cfg_file.write_text(json.dumps({"max_turns": 25}), encoding="utf-8")
    assert reloader.changed() is True

    # Clear changed
    reloader.clear_changed()
    assert reloader.changed() is False
    reloader.stop()


def test_config_reloader_callbacks_on_watcher_change(tmp_path: Path):
    agent_dir = tmp_path / ".codingAgent"
    agent_dir.mkdir(parents=True)
    cfg_file = agent_dir / "config.json"
    cfg_file.write_text(json.dumps({"val": 1}), encoding="utf-8")

    reloader = ConfigReloader(initial_load=False, workdir=tmp_path)
    called_with = []

    def on_change(paths):
        called_with.append(paths)

    reloader.add_callback(on_change)

    # Simulate change detection
    cfg_file.write_text(json.dumps({"val": 2}), encoding="utf-8")
    changed = reloader._check_for_changes()
    assert len(changed) > 0
    reloader._invoke_callbacks(changed)

    assert len(called_with) == 1
    assert str(cfg_file.resolve()) in list(called_with[0])
    reloader.stop()


def test_reload_if_pending_updates_global_config(tmp_path: Path):
    agent_dir = tmp_path / ".codingAgent"
    agent_dir.mkdir(parents=True)
    cfg_file = agent_dir / "config.json"
    cfg_file.write_text(json.dumps({"max_turns": 15}), encoding="utf-8")

    reloader = ConfigReloader(initial_load=False, workdir=tmp_path)
    with patch("src.core.config_hot_reload.get_config_reloader", return_value=reloader):
        with patch("src.core.config_loader._get_config_reloader", return_value=reloader):
            # Prime global config
            with patch.object(config_loader, "_get_workspace_config_paths", return_value=[cfg_file]):
                config_loader.reload_config(working_dir=tmp_path)
                assert config_loader.get("max_turns", working_dir=tmp_path) == 15

                # Modify file on disk
                cfg_file.write_text(json.dumps({"max_turns": 30}), encoding="utf-8")
                assert reloader.changed() is True

                # reload_if_pending should pick up the change
                reloaded = config_loader.reload_if_pending()
                assert reloaded is not None
                assert reloaded.get("max_turns") == 30
    reloader.stop()


def test_instruction_loader_reconciliation(tmp_path: Path):
    # Both .codingAgent and .agent instruction paths are in filenames
    assert any(".codingAgent" in f for f in _INSTRUCTION_FILENAMES)
    assert any(".agent" in f for f in _INSTRUCTION_FILENAMES)

    # Verify discovery from .codingAgent/AGENT.md
    agent_dir = tmp_path / ".codingAgent"
    agent_dir.mkdir(parents=True)
    instr_file = agent_dir / "AGENT.md"
    instr_file.write_text("Project instructions in .codingAgent", encoding="utf-8")

    result = load_project_instructions(tmp_path)
    assert "Project instructions in .codingAgent" in result
