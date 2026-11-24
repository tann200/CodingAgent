"""Regression tests for TASK-16: tool dispatchers, CLI hardening, config unification, and UI offloading."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. _tool.py parameter annotation inspection
# ---------------------------------------------------------------------------


def test_tool_schema_resolves_optional_and_pep604_unions():
    from src.tools._tool import ToolDefinition, PermissionKind

    def sample(
        count: int | None = None,
        flag: bool | None = None,
        items: list[str] | None = None,
        mapping: dict[str, int] | None = None,
        ratio: float = 1.0,
    ) -> dict:
        """Sample function with PEP 604 unions."""
        return {}

    t = ToolDefinition(name="sample", fn=sample, permission_kind=PermissionKind.READ_FILE)
    props = t.to_openai_schema()["function"]["parameters"]["properties"]
    assert props["count"]["type"] == "integer"
    assert props["flag"]["type"] == "boolean"
    assert props["items"]["type"] == "array"
    assert props["mapping"]["type"] == "object"
    assert props["ratio"]["type"] == "number"


# ---------------------------------------------------------------------------
# 2. Retired capability_tools.py & canonical git/verify primitives
# ---------------------------------------------------------------------------


def test_canonical_git_and_verify_primitives_permissions(tmp_path: Path):
    from src.tools import git_tools, verification_tools
    from src.tools._tool import PermissionKind

    assert git_tools.git_commit.__tool_meta__.permission_kind == PermissionKind.GIT_WRITE
    assert "write" in git_tools.git_commit.__tool_meta__.side_effects

    target = tmp_path / "ok.py"
    target.write_text("x = 1\n", encoding="utf-8")
    res = verification_tools.syntax_check(workdir=str(tmp_path))
    assert res.get("ok") is True or res.get("status") == "ok"


# ---------------------------------------------------------------------------
# 3. _bash_exec.py Popen & .sb cleanup tracking
# ---------------------------------------------------------------------------


def test_check_background_task_cleans_up_sb_profile(tmp_path: Path):
    from src.tools import _bash_exec

    sb_file = tmp_path / "test_profile.sb"
    sb_file.write_text("(version 1)", encoding="utf-8")
    fake_proc = MagicMock()
    fake_proc.poll.return_value = 0
    pid = 998877

    _bash_exec._SPAWNED_BG_PIDS.add(pid)
    _bash_exec._SPAWNED_BG_PROCS[pid] = (fake_proc, str(sb_file))

    res = _bash_exec.check_background_task(task_id=str(pid))
    assert res["status"] == "ok"
    assert res["running"] is False
    assert res["exit_code"] == 0
    assert not sb_file.exists()
    assert pid not in _bash_exec._SPAWNED_BG_PROCS
    assert pid not in _bash_exec._SPAWNED_BG_PIDS


# ---------------------------------------------------------------------------
# 4. src/main.py hook env docs & config.json / settings.json unification
# ---------------------------------------------------------------------------


def test_init_and_validate_config_unified(tmp_path: Path, capsys):
    from src.main import _run_init, _run_validate_config

    rc = _run_init(str(tmp_path), force=False)
    assert rc == 0

    pre_hook = (tmp_path / ".codingAgent" / "hooks" / "pre_tool.sh").read_text(encoding="utf-8")
    post_hook = (tmp_path / ".codingAgent" / "hooks" / "post_tool.sh").read_text(encoding="utf-8")
    assert "TOOL_ARGS_JSON" in pre_hook
    assert "TOOL_RESULT_JSON" in post_hook

    rc2 = _run_validate_config(str(tmp_path))
    assert rc2 == 0
    out = capsys.readouterr().out
    assert "[unknown key]" not in out
    assert "max_turns" in out


def test_load_project_settings_reads_config_json_and_overrides_with_settings(tmp_path: Path):
    from src.core.orchestration.project_settings import load_project_settings

    cfg_dir = tmp_path / ".codingAgent"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"max_turns": 42, "permission_mode": "read_only"}),
        encoding="utf-8",
    )
    ps = load_project_settings(tmp_path)
    assert ps.max_turns == 42
    assert ps.permission_mode == "read_only"

    (cfg_dir / "settings.json").write_text(
        json.dumps({"maxTurns": 15}),
        encoding="utf-8",
    )
    ps2 = load_project_settings(tmp_path)
    assert ps2.max_turns == 15


# ---------------------------------------------------------------------------
# 5. skill_cmd.py path traversal & YAML frontmatter safety
# ---------------------------------------------------------------------------


def test_skill_export_blocks_path_traversal_and_serializes_yaml(tmp_path: Path):
    from src.cli.skill_cmd import resolve_export_destination, export_single_skill

    with pytest.raises(ValueError):
        resolve_export_destination("../../evil", "agensi", out_dir=str(tmp_path))

    with pytest.raises(ValueError):
        resolve_export_destination("foo/bar", "claude", out_dir=str(tmp_path))

    skill_file = tmp_path / "src_skill.md"
    skill_file.write_text(
        "---\nname: tricky: skill\ndescription: \"Line 1: hello\\nLine 2\"\n---\nBody text\n",
        encoding="utf-8",
    )
    with patch("src.cli.skill_cmd._find_skill_file", return_value=skill_file):
        res = export_single_skill("tricky_skill", "agensi", out_dir=str(tmp_path))

    assert res["status"] == "exported"
    exported = Path(res["export_path"]).read_text(encoding="utf-8")
    import yaml

    parts = exported.split("---", 2)
    parsed = yaml.safe_load(parts[1])
    assert parsed["name"] == "tricky: skill"


# ---------------------------------------------------------------------------
# 6. doctor_cmd.py LiteLLM classification & credential status badges
# ---------------------------------------------------------------------------


def test_doctor_classifies_litellm_and_cloud_credentials(monkeypatch):
    from src.cli import doctor_cmd

    fake_configs = [
        {"name": "LiteLLM", "type": "litellm", "base_url": "http://localhost:4000", "models": ["gpt-4o"]},
        {"name": "Groq", "type": "groq", "models": ["llama-3.3-70b"]},
        {"name": "Anthropic", "type": "anthropic", "api_key": "sk-ant-test", "models": ["claude-sonnet-4"]},
    ]
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(doctor_cmd, "load_providers_config", lambda: fake_configs)
    monkeypatch.setattr(doctor_cmd, "diagnose_hardware_profile", lambda: {"ram_gb": 16, "vram_gb": 16, "cpu_cores": 8, "cpu_threads": 8, "machine": "arm64", "recommended_context": {"compact_9b_14b": 32768, "standard_15b_70b": 32768}})
    monkeypatch.setattr(doctor_cmd, "diagnose_sandbox_status", lambda: {"level": "workspace", "enforced": True, "available": True})
    monkeypatch.setattr(doctor_cmd, "check_endpoint_reachability", lambda url, timeout=2.0: (False, None, "offline"))
    import tui.tui_src.ui.config_writer as _cw

    monkeypatch.setattr(
        _cw,
        "load_provider_credentials",
        lambda provider_id: {},
    )

    diag = doctor_cmd.build_doctor_diagnosis(None)
    by_name = {p["provider"]: p for p in diag["providers"]}

    # LiteLLM routed through openai_compat (offline rather than configured cloud)
    assert by_name["litellm"]["type"] == "openai_compat"
    assert by_name["litellm"]["status"] == "offline"

    # Groq missing credentials -> no_credentials, Anthropic -> configured
    assert by_name["groq"]["status"] == "no_credentials"
    assert by_name["anthropic"]["status"] == "configured"

    markup = doctor_cmd.format_doctor_tui_markup(diag)
    assert "[NO CREDENTIALS]" in markup
    assert "[CONFIGURED]" in markup


# ---------------------------------------------------------------------------
# 7. UI offloading, local provider recognition & session snapshot git_sha
# ---------------------------------------------------------------------------


def test_bridge_defers_provider_probe_during_orchestrator_init(tmp_path: Path):
    from src.core.orchestration.orchestrator import Orchestrator
    from src.core.orchestration.event_bus import EventBus

    with patch("src.core.orchestration.orchestrator_provider_init._ensure_provider_manager_initialized_sync") as mock_init:
        Orchestrator(
            working_dir=tmp_path,
            event_bus=EventBus(),
            defer_provider_probe=True,
        )
        mock_init.assert_called_once_with(probe=False)


def test_chat_mixin_list_workspace_files_non_blocking_and_depth_capped(tmp_path: Path):
    from tui.tui_src.ui.components.chat_mixin import ChatDisplayMixin

    deep = tmp_path
    for i in range(9):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (deep / "too_deep.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "top.py").write_text("y = 2\n", encoding="utf-8")

    host = ChatDisplayMixin()
    host._bridge = SimpleNamespace(working_dir=str(tmp_path))
    host._at_file_cache = []
    host._at_file_cache_ts = 0.0
    host._at_file_scanning = False

    results = host._list_workspace_files("")
    assert "top.py" in results
    assert not any("too_deep.py" in r for r in results)


def test_app_session_snapshot_records_git_sha_and_revert_uses_thread(tmp_path: Path, monkeypatch):
    from tui.tui_src.ui._app_session_mixin import AppSessionMixin
    from tui.tui_src.ui.screens.session_screen import SessionScreen

    class FakeApp(AppSessionMixin):
        def __init__(self):
            self._session_id = "sess1234"
            self._session_custom_title = None
            self._bridge = SimpleNamespace(
                _history_lock=MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None),
                history=[("user", "hello")],
                working_dir=str(tmp_path),
                get_turn_count=lambda: 1,
                get_usage_totals=lambda: (10, 5),
            )

        def _get_sessions_dir(self) -> Path:
            return tmp_path

    app = FakeApp()
    with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="deadbeef1234\n")):
        app._save_session_snapshot()

    saved = json.loads((tmp_path / "session_sess1234.json").read_text(encoding="utf-8"))
    assert saved["git_sha"] == "deadbeef1234"

    # Verify SessionScreen._do_revert offloads subprocess.run to a thread
    screen = SessionScreen()
    mock_app = MagicMock()
    monkeypatch.setattr(type(screen), "app", property(lambda self: mock_app))
    monkeypatch.setattr(screen, "dismiss", lambda *a, **kw: None)

    started = []
    with patch("threading.Thread") as mock_thread:
        mock_thread.side_effect = lambda target, daemon=True: started.append((target, daemon)) or MagicMock()
        screen._do_revert("deadbeef", str(tmp_path))

    assert len(started) == 1
    assert started[0][1] is True


def test_bridge_provider_local_types_include_mlx_llama_server_litellm():
    import inspect
    from tui.tui_src.ui._bridge_provider import BridgeProviderMixin

    src_text = inspect.getsource(BridgeProviderMixin._publish_active_provider_status)
    for key in ("mlx", "llama_server", "vllm", "litellm"):
        assert key in src_text
