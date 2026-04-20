"""Regression tests for the remaining roadmap gap implementations.

Covers:
  SB-1   sandbox.run_sandboxed falls back to plain subprocess when bwrap absent
  SB-2   sandbox.sandbox_available() returns bool
  SB-3   set_sandbox_level rejects invalid level
  SB-4   sandbox level "off" bypasses bwrap even if available
  AL-1   TOOL_ALIASES maps short names to canonical names
  AL-2   execute_tool normalises alias names (mocked registry)
  PL-1   PermissionLevel enum has READ_ONLY / WORKSPACE_WRITE / DANGER / PROMPT / ALLOW
  PL-2   get_tool_permission returns READ_ONLY for read_file
  PL-3   get_tool_permission returns DANGER for bash
  PL-4   set_tool_permission overrides a tool's level
  TL-1   turn_count / max_turns keys present in AgentState TypedDict
  TL-2   perception_node increments turn_count in result
  TL-3   perception_node returns turn_limit_reached when turn_count > max_turns
  CF-1   load_merged_config returns dict (no raises)
  CF-2   workspace layer overrides bundled default
  CF-3   deep merge combines nested dicts
  CF-4   config_loader.get() shortcut works
  PL-R1  load_plugin_tools in build_registry via plugin_tools config key
  OF-1   main._parse_args recognises --output-format choices
  OF-2   main._parse_args recognises --task and --workdir
  OF-3   main._parse_args recognises --sandbox-level
  LSP-1  get_lsp_context_block returns empty when disabled
  LSP-2  get_lsp_context_block returns empty when no symbol graph
  VC-1   --validate-config flag parsed by _parse_args
  VC-2   main() dispatches --validate-config and exits 0 when no files exist
  VC-3   _run_validate_config reports unknown keys
  VC-4   _run_validate_config exits 1 on invalid JSON
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# SB — sandbox
# ---------------------------------------------------------------------------


def test_sandbox_available_returns_bool():
    from src.tools.sandbox import sandbox_available

    assert isinstance(sandbox_available(), bool)


def test_sandbox_fallback_runs_command(tmp_path):
    """When bwrap is absent sandbox falls back to subprocess.run and works."""
    from src.tools import sandbox as _sb

    original = _sb._BWRAP_PATH
    _sb._BWRAP_PATH = None  # simulate bwrap not installed
    try:
        result = _sb.run_sandboxed(
            [sys.executable, "-c", "print('hello')"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "hello"
    finally:
        _sb._BWRAP_PATH = original


def test_set_sandbox_level_rejects_invalid():
    from src.tools.sandbox import set_sandbox_level

    with pytest.raises(ValueError):
        set_sandbox_level("ultra")


def test_sandbox_level_off_skips_bwrap(tmp_path, monkeypatch):
    """sandbox_level="off" must not invoke bwrap even when bwrap is on PATH."""
    from src.tools import sandbox as _sb

    called = []
    original_run = __import__("subprocess").run

    def _spy_run(cmd, **kw):
        called.append(cmd)
        return original_run(cmd, **kw)

    monkeypatch.setattr(_sb, "_BWRAP_PATH", "/usr/bin/bwrap")
    monkeypatch.setattr(__import__("subprocess"), "run", _spy_run)
    _sb.run_sandboxed(
        [sys.executable, "-c", "pass"],
        cwd=tmp_path,
        sandbox_level="off",
        capture_output=True,
        text=True,
    )
    assert all("bwrap" not in str(c[0]) for c in called if isinstance(c, list))


# ---------------------------------------------------------------------------
# AL — tool aliases
# ---------------------------------------------------------------------------


def test_tool_aliases_short_names():
    from src.tools.tools_config import TOOL_ALIASES

    assert TOOL_ALIASES["read"] == "read_file"
    assert TOOL_ALIASES["write"] == "write_file"
    assert TOOL_ALIASES["edit"] == "edit_file_atomic"
    assert TOOL_ALIASES["ls"] == "list_files"
    assert TOOL_ALIASES["find"] == "glob"
    assert TOOL_ALIASES["shell"] == "bash"


def test_tool_alias_normalisation_in_execute_tool():
    """execute_tool must resolve alias names before tool lookup."""
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.tool_registry = MagicMock()
    # Simulate 'read' alias resolving to read_file in registry
    orch.tool_registry.get.return_value = {
        "fn": lambda **kw: {"status": "ok", "content": "data"},
        "side_effects": [],
        "tags": [],
    }
    orch.working_dir = Path(".")
    orch.event_bus = MagicMock()
    orch.event_bus.publish = MagicMock()
    orch._session_read_files = set()
    orch._session_modified_files = set()
    orch._usage_buffer = {}
    orch._step_snapshot_id = None
    orch.plan_mode = None  # type: ignore[attr-defined]
    orch.explore_mode = False
    orch.cancel_event = None
    orch.current_role = None
    orch._tool_hook_runner = None  # type: ignore[attr-defined]
    orch.rollback_manager = MagicMock()
    orch.rollback_manager.snapshot_files = MagicMock(return_value="snap1")
    orch.rollback_manager.append_to_snapshot = MagicMock()

    result = orch.execute_tool({"name": "read", "arguments": {"path": "foo.py"}})
    # The lookup must have been called with 'read_file', not 'read'
    call_args = orch.tool_registry.get.call_args_list
    looked_up = [c.args[0] for c in call_args]
    assert "read_file" in looked_up


# ---------------------------------------------------------------------------
# PL — PermissionLevel
# ---------------------------------------------------------------------------


def test_permission_level_enum_values():
    from src.tools.tools_config import PermissionLevel

    assert PermissionLevel.READ_ONLY.value == "read_only"
    assert PermissionLevel.WORKSPACE_WRITE.value == "workspace_write"
    assert PermissionLevel.DANGER.value == "danger"
    assert PermissionLevel.PROMPT.value == "prompt"
    assert PermissionLevel.ALLOW.value == "allow"


def test_read_file_is_read_only():
    from src.tools.tools_config import get_tool_permission, PermissionLevel

    assert get_tool_permission("read_file") == PermissionLevel.READ_ONLY


def test_bash_is_danger():
    from src.tools.tools_config import get_tool_permission, PermissionLevel

    assert get_tool_permission("bash") == PermissionLevel.DANGER


def test_set_tool_permission_overrides():
    from src.tools.tools_config import (
        set_tool_permission,
        get_tool_permission,
        PermissionLevel,
    )

    set_tool_permission("my_custom_tool", PermissionLevel.READ_ONLY)
    assert get_tool_permission("my_custom_tool") == PermissionLevel.READ_ONLY
    # Cleanup
    from src.tools import tools_config

    tools_config.TOOL_PERMISSIONS.pop("my_custom_tool", None)


# ---------------------------------------------------------------------------
# TL — turn limit
# ---------------------------------------------------------------------------


def test_turn_count_in_agent_state():
    from src.core.orchestration.graph.state import AgentState

    hints = AgentState.__annotations__
    assert "turn_count" in hints
    assert "max_turns" in hints


def test_turn_count_max_turns_in_initial_state():
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    # Simulate minimal orchestrator attributes
    orch.working_dir = Path(".")
    orch._session_read_files = set()
    orch._usage_buffer = {}
    orch._current_task_id = "test"
    orch.cancel_event = None
    orch.deterministic = False
    orch.seed = None
    orch.plan_mode = None  # type: ignore[attr-defined]
    orch.file_lock_manager = None  # type: ignore[attr-defined]
    orch.agent_session_manager = None  # type: ignore[attr-defined]
    orch.context_controller = None  # type: ignore[attr-defined]
    orch._adapter = None
    # Build a minimal initial_state via the orchestrator code path
    # (without triggering LangGraph) — just check the keys exist
    state_keys = ["turn_count", "max_turns"]
    # Read the source to verify they're in the initial_state dict
    import inspect

    src_text = ""
    if hasattr(orch, "run_agent_once"):
        import src.core.orchestration.inference_loop as _il_mod

        src_text = inspect.getsource(orch.run_agent_once) + inspect.getsource(_il_mod)
    for key in state_keys:
        assert f'"{key}"' in src_text or f"'{key}'" in src_text, (
            f"Missing {key!r} in run_agent_once"
        )


@pytest.mark.asyncio
async def test_perception_node_turn_limit():
    """perception_node returns turn_limit_reached when turn_count > max_turns."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    orch = MagicMock()
    orch.adapter = MagicMock()
    orch.cancel_event = None
    orch.event_bus = MagicMock()
    orch.event_bus.publish = MagicMock()

    state = {
        "turn_count": 50,  # will be incremented to 51 > max_turns=50
        "max_turns": 50,
        "rounds": 0,
        "history": [],
        "task": "test",
        "errors": [],
        "next_action": None,
        "last_result": None,
        "working_dir": ".",
        "cancel_event": None,
    }

    config = {"configurable": {"orchestrator": orch}}

    result = await perception_node(state, config)
    assert "turn_limit_reached" in result.get("errors", [])
    assert result["turn_count"] == 51


# ---------------------------------------------------------------------------
# CF — config loader
# ---------------------------------------------------------------------------


def test_load_merged_config_returns_dict(tmp_path):
    from src.core.config_loader import load_merged_config

    cfg = load_merged_config(working_dir=tmp_path)
    assert isinstance(cfg, dict)


def test_workspace_layer_overrides_default(tmp_path):
    from src.core.config_loader import load_merged_config

    workspace_cfg = tmp_path / ".agent" / "config.json"
    workspace_cfg.parent.mkdir(parents=True)
    workspace_cfg.write_text(json.dumps({"max_turns": 99}))
    cfg = load_merged_config(working_dir=tmp_path)
    assert cfg.get("max_turns") == 99


def test_deep_merge_combines_nested_dicts():
    from src.core.config_loader import _deep_merge

    base = {"a": {"x": 1, "y": 2}, "b": 10}
    override = {"a": {"y": 99, "z": 3}, "c": 20}
    result = _deep_merge(base, override)
    assert result["a"] == {"x": 1, "y": 99, "z": 3}
    assert result["b"] == 10
    assert result["c"] == 20


def test_config_loader_get_shortcut(tmp_path):
    from src.core.config_loader import get as cfg_get

    result = cfg_get("nonexistent_key_xyz", default="fallback", working_dir=tmp_path)
    assert result == "fallback"


def test_local_layer_overrides_workspace(tmp_path):
    from src.core.config_loader import load_merged_config

    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"max_turns": 50}))
    (agent_dir / "config.local.json").write_text(json.dumps({"max_turns": 25}))
    cfg = load_merged_config(working_dir=tmp_path)
    assert cfg.get("max_turns") == 25


# ---------------------------------------------------------------------------
# SM-1 — small_model config field
# ---------------------------------------------------------------------------


def test_get_small_model_returns_none_by_default(tmp_path, monkeypatch):
    """get_small_model() returns None when no small_model is configured."""
    from src.core.config_loader import get_small_model

    # Point bundled defaults at an empty providers list so no active provider
    import json as _json
    from pathlib import Path
    import src.core.config_loader as _cl

    dummy_providers = Path(tmp_path / "providers.json")
    dummy_providers.write_text(_json.dumps([]), encoding="utf-8")
    monkeypatch.setattr(_cl, "_BUNDLED_DEFAULTS", dummy_providers)
    monkeypatch.setattr(_cl, "_USER_CONFIG", tmp_path / "no_such.json")

    result = get_small_model(working_dir=tmp_path)
    assert result is None


def test_get_small_model_reads_from_workspace_config(tmp_path, monkeypatch):
    """get_small_model() returns value from workspace .agent/config.json."""
    from src.core.config_loader import get_small_model

    import json as _json
    from pathlib import Path
    import src.core.config_loader as _cl

    dummy_providers = Path(tmp_path / "providers.json")
    dummy_providers.write_text(_json.dumps([]), encoding="utf-8")
    monkeypatch.setattr(_cl, "_BUNDLED_DEFAULTS", dummy_providers)
    monkeypatch.setattr(_cl, "_USER_CONFIG", tmp_path / "no_such.json")

    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        _json.dumps({"small_model": "gpt-4o-mini"}), encoding="utf-8"
    )

    result = get_small_model(working_dir=tmp_path)
    assert result == "gpt-4o-mini"


def test_get_small_model_reads_from_active_provider(tmp_path, monkeypatch):
    """get_small_model() reads small_model from the active provider entry."""
    from src.core.config_loader import get_small_model

    import json as _json
    from pathlib import Path
    import src.core.config_loader as _cl

    providers_data = [
        {"name": "test_prov", "active": True, "small_model": "gemini-2.0-flash-001"},
    ]
    dummy_providers = Path(tmp_path / "providers.json")
    dummy_providers.write_text(_json.dumps(providers_data), encoding="utf-8")
    monkeypatch.setattr(_cl, "_BUNDLED_DEFAULTS", dummy_providers)
    monkeypatch.setattr(_cl, "_USER_CONFIG", tmp_path / "no_such.json")

    result = get_small_model(working_dir=tmp_path)
    assert result == "gemini-2.0-flash-001"


# ---------------------------------------------------------------------------
# PL-R1 — plugin tool loading via config
# ---------------------------------------------------------------------------


def test_plugin_tools_loaded_via_config(tmp_path, monkeypatch):
    """build_registry discovers modules listed in plugin_tools config key."""
    import types
    from src.tools._tool import tool
    from src.tools._registry import build_registry

    # Create a fake plugin module with a @tool function
    fake_mod = types.ModuleType("fake_plugin_tools")

    @tool(tags=["coding"])
    def my_plugin_tool(text: str) -> dict:
        """A test plugin tool."""
        return {"status": "ok", "text": text}

    fake_mod.my_plugin_tool = my_plugin_tool  # type: ignore[attr-defined]

    # Monkeypatch config_loader.get to return the fake module's path
    import sys

    sys.modules["fake_plugin_tools"] = fake_mod

    with patch("src.core.config_loader.get", return_value=["fake_plugin_tools"]):
        reg = build_registry(working_dir=str(tmp_path))

    assert "my_plugin_tool" in reg
    sys.modules.pop("fake_plugin_tools", None)


# ---------------------------------------------------------------------------
# OF — output format CLI args
# ---------------------------------------------------------------------------


def test_parse_args_output_format():
    from src.main import _parse_args

    args = _parse_args(["--output-format", "json"])
    assert args.output_format == "json"


def test_parse_args_task_and_workdir():
    from src.main import _parse_args

    args = _parse_args(["--task", "write a test", "--workdir", "/tmp"])
    assert args.task == "write a test"
    assert args.workdir == "/tmp"


def test_parse_args_sandbox_level():
    from src.main import _parse_args

    args = _parse_args(["--sandbox-level", "full"])
    assert args.sandbox_level == "full"


def test_parse_args_default_format():
    from src.main import _parse_args

    args = _parse_args([])
    assert args.output_format == "pretty"


# ---------------------------------------------------------------------------
# LSP — lsp_context
# ---------------------------------------------------------------------------


def test_lsp_context_disabled_by_default(tmp_path):
    from src.core.indexing.lsp_context import get_lsp_context_block

    # Default: feature is off, no env var set
    result = get_lsp_context_block(workdir=tmp_path)
    assert result == ""


def test_lsp_context_empty_when_no_index(tmp_path, monkeypatch):
    from src.core.indexing import lsp_context

    monkeypatch.setattr(lsp_context, "_is_enabled", lambda: True)
    result = lsp_context.get_lsp_context_block(workdir=tmp_path)
    assert result == ""


# ---------------------------------------------------------------------------
# VC — --validate-config CLI flag (UX-2)
# ---------------------------------------------------------------------------


def test_parse_args_validate_config_flag():
    """VC-1: _parse_args recognises --validate-config and sets the attribute."""
    from src.main import _parse_args

    args = _parse_args(["--validate-config"])
    assert getattr(args, "validate_config", False) is True


def test_main_validate_config_no_files(tmp_path, capsys):
    """VC-2: main() dispatches --validate-config; exits 0 when no settings files exist."""
    from src.main import main

    ret = main(["--validate-config", "--workdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert ret == 0
    assert "No settings files found" in out or ".agent" in out


def test_run_validate_config_unknown_key(tmp_path, capsys):
    """VC-3: _run_validate_config flags keys that are not in the known set."""
    from src.main import _run_validate_config

    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    sf = agent_dir / "settings.json"
    sf.write_text('{"unknownFutureKey": true, "maxTurns": 10}', encoding="utf-8")

    ret = _run_validate_config(str(tmp_path))
    out = capsys.readouterr().out
    assert ret == 0
    assert "unknownFutureKey" in out
    assert "[unknown key]" in out
    # Known key should NOT be flagged
    assert "maxTurns" in out
    lines = [l for l in out.splitlines() if "maxTurns" in l]
    assert all("[unknown key]" not in l for l in lines)


def test_run_validate_config_invalid_json(tmp_path, capsys):
    """VC-4: _run_validate_config exits 1 when the settings file is not valid JSON."""
    from src.main import _run_validate_config

    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    sf = agent_dir / "settings.json"
    sf.write_text("{not valid json", encoding="utf-8")

    ret = _run_validate_config(str(tmp_path))
    assert ret == 1


# ---------------------------------------------------------------------------
# DR — --dry-run CLI flag (UX-3)
# ---------------------------------------------------------------------------


def test_parse_args_dry_run_flag():
    """DR-1: _parse_args recognises --dry-run and sets the attribute."""
    from src.main import _parse_args

    args = _parse_args(["--dry-run", "--task", "do something"])
    assert getattr(args, "dry_run", False) is True


def test_dry_run_blocked_tools_set():
    """DR-2: DRY_RUN_BLOCKED_TOOLS includes write tools and bash."""
    from src.core.orchestration.orchestrator import DRY_RUN_BLOCKED_TOOLS

    for tool in ("edit_file", "write_file", "bash", "delete_file"):
        assert tool in DRY_RUN_BLOCKED_TOOLS, f"{tool!r} not in DRY_RUN_BLOCKED_TOOLS"


def test_orchestrator_dry_run_intercepts_write(tmp_path):
    """DR-3: In dry-run mode execute_tool returns a preview dict for write tools."""
    from unittest.mock import MagicMock

    from src.core.orchestration.orchestrator import Orchestrator

    orch: Any = object.__new__(Orchestrator)
    orch._dry_run = True
    orch.working_dir = tmp_path
    orch._session_read_files = set()
    orch._plan_mode_approved = None
    orch._affected_files = []
    orch.plan_mode = None
    orch.event_bus = MagicMock()
    orch._tool_hook_runner = None
    orch._hooks = {}

    result = orch.execute_tool(
        {"name": "edit_file", "arguments": {"path": "foo.py", "content": "x"}}
    )
    assert result.get("status") == "dry_run"
    assert result.get("would_call") == "edit_file"


def test_orchestrator_dry_run_intercepts_bash(tmp_path):
    """DR-4: In dry-run mode execute_tool returns a preview dict for bash."""
    from unittest.mock import MagicMock

    from src.core.orchestration.orchestrator import Orchestrator

    orch: Any = object.__new__(Orchestrator)
    orch._dry_run = True
    orch.working_dir = tmp_path
    orch._session_read_files = set()
    orch._plan_mode_approved = None
    orch._affected_files = []
    orch.plan_mode = None
    orch.event_bus = MagicMock()
    orch._tool_hook_runner = None
    orch._hooks = {}

    result = orch.execute_tool({"name": "bash", "arguments": {"command": "rm -rf /"}})
    assert result.get("status") == "dry_run"
    assert result.get("would_call") == "bash"


def test_orchestrator_dry_run_does_not_block_read(tmp_path):
    """DR-5: In dry-run mode read_file is NOT blocked (read-only tools still run)."""
    from unittest.mock import MagicMock

    from src.core.orchestration.orchestrator import Orchestrator

    orch: Any = object.__new__(Orchestrator)
    orch._dry_run = True
    orch.working_dir = tmp_path
    orch._session_read_files = set()
    orch._plan_mode_approved = None
    orch._affected_files = []
    orch.plan_mode = None
    orch.event_bus = MagicMock()
    orch._tool_hook_runner = None
    orch._hooks = {}
    # Minimal tool registry so execute_tool proceeds past the dry-run guard.
    mock_registry = MagicMock()
    mock_registry.get.return_value = None  # tool "not found" path
    orch.tool_registry = mock_registry

    # For read_file, dry-run must NOT short-circuit to a preview dict.
    # With no tool registered it will return an "ok": False / not-found result,
    # but critically the status must not be "dry_run".
    result = orch.execute_tool(
        {"name": "read_file", "arguments": {"path": str(tmp_path)}}
    )
    assert result.get("status") != "dry_run"
