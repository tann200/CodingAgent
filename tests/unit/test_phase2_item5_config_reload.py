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


def test_load_merged_config_does_not_prematurely_clear_changed(tmp_path: Path):
    """Calling load_merged_config() directly must not clear reloader.changed()."""
    agent_dir = tmp_path / ".codingAgent"
    agent_dir.mkdir(parents=True)
    cfg_file = agent_dir / "config.json"
    cfg_file.write_text(json.dumps({"max_turns": 10}), encoding="utf-8")

    reloader = ConfigReloader(initial_load=False, workdir=tmp_path)
    with patch("src.core.config_hot_reload.get_config_reloader", return_value=reloader):
        with patch("src.core.config_loader._get_config_reloader", return_value=reloader):
            with patch.object(config_loader, "_get_workspace_config_paths", return_value=[cfg_file]):
                # Initially unchanged
                assert reloader.changed() is False

                # Modify file on disk
                cfg_file.write_text(json.dumps({"max_turns": 42}), encoding="utf-8")
                assert reloader.changed() is True

                # Call load_merged_config directly (e.g. via get_mcp_config)
                config_loader.load_merged_config(working_dir=tmp_path)

                # Reloader must STILL be changed so global config reload is not lost
                assert reloader.changed() is True

                # get_global_config should pick it up and then clear
                cfg = config_loader.get_global_config()
                assert cfg.get("max_turns") == 42
                assert reloader.changed() is False
    reloader.stop()


def test_instruction_loader_and_files_discover_agents_md_and_redact(tmp_path: Path):
    """Both instruction loaders discover AGENTS.md, stop at .git, and redact secrets."""
    from src.core.context.instruction_files import discover_instruction_files

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    agents_file = tmp_path / "AGENTS.md"
    secret_key = "sk-ant-api03-abcdef1234567890abcdef1234567890"
    agents_file.write_text(f"Instructions with secret: {secret_key}", encoding="utf-8")

    child = tmp_path / "subdir"
    child.mkdir()

    # load_project_instructions discovers AGENTS.md and redacts secrets
    res_loader = load_project_instructions(child)
    assert "Instructions with secret" in res_loader
    assert secret_key not in res_loader
    assert "[REDACTED" in res_loader

    # discover_instruction_files discovers AGENTS.md and redacts secrets
    res_files = discover_instruction_files(child)
    assert len(res_files) >= 1
    matched = [f for f in res_files if f.path.name == "AGENTS.md"]
    assert len(matched) == 1
    assert "Instructions with secret" in matched[0].content
    assert secret_key not in matched[0].content
    assert "[REDACTED" in matched[0].content


def test_orchestrator_config_reload_preserves_overrides_and_reloads_providers(tmp_path: Path):
    """_on_config_reloaded must re-apply project overrides and reload ProviderManager."""
    from src.core.orchestration.orchestrator_config_reload import _on_config_reloaded
    from src.core.inference.llm_manager import get_provider_manager

    orch = MagicMock()
    orch.working_dir = tmp_path
    orch.tool_registry = MagicMock()
    orch.event_bus = MagicMock()

    pm = get_provider_manager()
    with patch.object(pm, "reload") as mock_pm_reload:
        with patch("src.core.config_loader.get_project_tool_overrides", return_value={"bash": False}):
            _on_config_reloaded(orch, [str(tmp_path / "providers.json")])

            # Verified: "bash" was deregistered on the new tool registry
            assert orch.tool_registry.get("bash") is None
            # Verified: pm.reload was triggered
            mock_pm_reload.assert_called_once()

