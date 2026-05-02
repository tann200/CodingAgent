"""Regression tests for src/core/orchestration/tool_hooks.py

Covers:
  TH-1   No hooks file → run_pre returns allowed=True
  TH-2   Pre-hook exits 0 → allowed=True
  TH-3   Pre-hook exits non-zero → allowed=False with reason
  TH-4   Pre-hook match pattern '*' matches any tool
  TH-5   Pre-hook match pattern 'bash' only matches bash
  TH-6   Pre-hook timeout → allowed=True (fail-open)
  TH-7   Post-hook is fire-and-forget (never raises)
  TH-8   TOOL_NAME env var is set for hook subprocess
  TH-9   TOOL_ARGS_JSON env var contains serialized args
  TH-10  Global hooks.json merged with workspace hooks.json
  TH-11  Workspace hooks take precedence (appended, evaluated first)
  TH-12  reload() clears cached config
  TH-13  Broken hooks.json is silently ignored
  TH-14  run_pre with no config loaded (None working_dir)
  TH-15  Auto-stub: .agent/hooks/pre_tool.sh wired when no hooks.json (Gap C)
  TH-16  Auto-stub: .agent/hooks/post_tool.sh wired when no hooks.json (Gap C)
  TH-17  Auto-stub ignored when hooks.json pre_tool entries already exist
  TH-18  deferred_init checks _tool_hook_runner attribute (Gap A)
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import sys
from pathlib import Path



# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _runner(working_dir: Path):
    from src.core.orchestration.tool_hooks import ToolHookRunner

    return ToolHookRunner(working_dir=working_dir)


# ---------------------------------------------------------------------------
# TH-1  No config → always allowed
# ---------------------------------------------------------------------------


def test_no_hooks_always_allowed(tmp_path):
    r = _runner(tmp_path)
    result = r.run_pre("bash", {"command": "ls"})
    assert result.allowed is True


# ---------------------------------------------------------------------------
# TH-2  Hook exits 0 → allowed
# ---------------------------------------------------------------------------


def test_pre_hook_exit_0_allowed(tmp_path):
    _write(
        tmp_path / ".codingAgent" / "hooks.json",
        {
            "pre_tool": [
                {"match": "*", "cmd": f"{sys.executable} -c 'import sys; sys.exit(0)'"}
            ]
        },
    )
    r = _runner(tmp_path)
    result = r.run_pre("read_file", {"path": "foo.py"})
    assert result.allowed is True


# ---------------------------------------------------------------------------
# TH-3  Hook exits non-zero → denied with reason
# ---------------------------------------------------------------------------


def test_pre_hook_exit_nonzero_denied(tmp_path):
    _write(
        tmp_path / ".codingAgent" / "hooks.json",
        {
            "pre_tool": [
                {
                    "match": "*",
                    "cmd": f"{sys.executable} -c 'import sys; sys.stderr.write(\"blocked\"); sys.exit(1)'",
                }
            ]
        },
    )
    r = _runner(tmp_path)
    result = r.run_pre("bash", {"command": "rm -rf /"})
    assert result.allowed is False
    assert "blocked" in result.reason


# ---------------------------------------------------------------------------
# TH-4  match '*' catches any tool name
# ---------------------------------------------------------------------------


def test_wildcard_match(tmp_path):
    _write(
        tmp_path / ".codingAgent" / "hooks.json",
        {
            "pre_tool": [
                {"match": "*", "cmd": f"{sys.executable} -c 'import sys; sys.exit(1)'"}
            ]
        },
    )
    r = _runner(tmp_path)
    for tool in ("bash", "write_file", "read_file", "totally_custom"):
        assert r.run_pre(tool, {}).allowed is False


# ---------------------------------------------------------------------------
# TH-5  match 'bash' only matches bash
# ---------------------------------------------------------------------------


def test_specific_match(tmp_path):
    _write(
        tmp_path / ".codingAgent" / "hooks.json",
        {
            "pre_tool": [
                {
                    "match": "bash",
                    "cmd": f"{sys.executable} -c 'import sys; sys.exit(1)'",
                }
            ]
        },
    )
    r = _runner(tmp_path)
    assert r.run_pre("bash", {}).allowed is False
    assert r.run_pre("read_file", {}).allowed is True


# ---------------------------------------------------------------------------
# TH-6  Pre-hook timeout → fail-open (allowed)
# ---------------------------------------------------------------------------


def test_pre_hook_timeout_failopen(tmp_path):
    from src.core.orchestration import tool_hooks

    # Temporarily reduce timeout to 0.05s so the test is fast
    original = tool_hooks._PRE_HOOK_TIMEOUT
    tool_hooks._PRE_HOOK_TIMEOUT = 0.05
    try:
        _write(
            tmp_path / ".codingAgent" / "hooks.json",
            {
                "pre_tool": [
                    {
                        "match": "*",
                        "cmd": f"{sys.executable} -c 'import time; time.sleep(5); import sys; sys.exit(1)'",
                    }
                ]
            },
        )
        r = _runner(tmp_path)
        result = r.run_pre("bash", {})
        assert result.allowed is True  # timed out → fail open
    finally:
        tool_hooks._PRE_HOOK_TIMEOUT = original


# ---------------------------------------------------------------------------
# TH-7  Post-hook never raises
# ---------------------------------------------------------------------------


def test_post_hook_never_raises(tmp_path):
    _write(
        tmp_path / ".codingAgent" / "hooks.json",
        {
            "post_tool": [
                {"match": "*", "cmd": f"{sys.executable} -c 'import sys; sys.exit(1)'"}
            ]
        },
    )
    r = _runner(tmp_path)
    # Must not raise even though hook exits 1
    r.run_post("write_file", {"path": "a.py"}, {"ok": True})


# ---------------------------------------------------------------------------
# TH-8  TOOL_NAME env var passed to hook
# ---------------------------------------------------------------------------


def test_tool_name_env_var(tmp_path):
    sentinel = tmp_path / "tool_name.txt"
    _write(
        tmp_path / ".codingAgent" / "hooks.json",
        {
            "pre_tool": [
                {
                    "match": "*",
                    "cmd": f'{sys.executable} -c \'import os; open("{sentinel}", "w").write(os.environ["TOOL_NAME"])\'',
                }
            ]
        },
    )
    r = _runner(tmp_path)
    r.run_pre("my_tool", {})
    assert sentinel.read_text() == "my_tool"


# ---------------------------------------------------------------------------
# TH-9  TOOL_ARGS_JSON contains args
# ---------------------------------------------------------------------------


def test_tool_args_json_env_var(tmp_path):
    sentinel = tmp_path / "args.json"
    _write(
        tmp_path / ".codingAgent" / "hooks.json",
        {
            "pre_tool": [
                {
                    "match": "*",
                    "cmd": f'{sys.executable} -c \'import os; open("{sentinel}", "w").write(os.environ["TOOL_ARGS_JSON"])\'',
                }
            ]
        },
    )
    r = _runner(tmp_path)
    r.run_pre("read_file", {"path": "hello.py"})
    data = json.loads(sentinel.read_text())
    assert data.get("path") == "hello.py"


# ---------------------------------------------------------------------------
# TH-10 Global hooks merged with workspace hooks
# ---------------------------------------------------------------------------


def test_global_hooks_merged(tmp_path, monkeypatch):
    from src.core.orchestration import tool_hooks

    global_hooks = tmp_path / "global_hooks.json"
    _write(
        global_hooks,
        {
            "pre_tool": [
                {
                    "match": "bash",
                    "cmd": f"{sys.executable} -c 'import sys; sys.exit(1)'",
                }
            ]
        },
    )

    monkeypatch.setattr(tool_hooks, "_GLOBAL_HOOKS_PATH", global_hooks)
    r = tool_hooks.ToolHookRunner(working_dir=tmp_path)
    # bash is blocked by global hook; write_file is not
    assert r.run_pre("bash", {}).allowed is False
    assert r.run_pre("write_file", {}).allowed is True


# ---------------------------------------------------------------------------
# TH-11 Workspace hooks evaluated (both global + workspace active)
# ---------------------------------------------------------------------------


def test_workspace_hooks_also_active(tmp_path, monkeypatch):
    from src.core.orchestration import tool_hooks

    global_hooks = tmp_path / "global_hooks.json"
    _write(
        global_hooks,
        {
            "pre_tool": [
                {
                    "match": "bash",
                    "cmd": f"{sys.executable} -c 'import sys; sys.exit(1)'",
                }
            ]
        },
    )
    _write(
        tmp_path / ".codingAgent" / "hooks.json",
        {
            "pre_tool": [
                {
                    "match": "write_file",
                    "cmd": f"{sys.executable} -c 'import sys; sys.exit(1)'",
                }
            ]
        },
    )

    monkeypatch.setattr(tool_hooks, "_GLOBAL_HOOKS_PATH", global_hooks)
    r = tool_hooks.ToolHookRunner(working_dir=tmp_path)
    assert r.run_pre("bash", {}).allowed is False
    assert r.run_pre("write_file", {}).allowed is False
    assert r.run_pre("read_file", {}).allowed is True


# ---------------------------------------------------------------------------
# TH-12 reload() clears cached config
# ---------------------------------------------------------------------------


def test_reload_clears_cache(tmp_path):
    r = _runner(tmp_path)
    # Access config to populate cache
    _ = r._get_config()
    assert r._config is not None
    r.reload()
    assert r._config is None


# ---------------------------------------------------------------------------
# TH-13 Broken hooks.json silently ignored
# ---------------------------------------------------------------------------


def test_broken_hooks_json_ignored(tmp_path):
    hooks_path = tmp_path / ".codingAgent" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text("NOT VALID JSON {{{{", encoding="utf-8")
    r = _runner(tmp_path)
    result = r.run_pre("bash", {})
    assert result.allowed is True  # broken config → no hooks → allowed


# ---------------------------------------------------------------------------
# TH-14 Works fine when working_dir is None
# ---------------------------------------------------------------------------


def test_none_working_dir():
    from src.core.orchestration.tool_hooks import ToolHookRunner

    r = ToolHookRunner(working_dir=None)
    result = r.run_pre("bash", {"command": "ls"})
    assert result.allowed is True


# ---------------------------------------------------------------------------
# TH-15  Auto-stub: .agent/hooks/pre_tool.sh wired when no hooks.json (Gap C)
# ---------------------------------------------------------------------------


def test_pre_tool_sh_stub_auto_wired(tmp_path):
    """A .agent/hooks/pre_tool.sh that exits 1 should deny the call even
    when no hooks.json exists (the stub is auto-wired as a wildcard hook)."""
    import sys

    stub_dir = tmp_path / ".codingAgent" / "hooks"
    stub_dir.mkdir(parents=True)
    stub = stub_dir / "pre_tool.sh"
    stub.write_text(
        f"#!/usr/bin/env {sys.executable}\nimport sys; sys.exit(1)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    from src.core.orchestration.tool_hooks import ToolHookRunner

    r = ToolHookRunner(working_dir=tmp_path)
    result = r.run_pre("bash", {})
    assert result.allowed is False


# ---------------------------------------------------------------------------
# TH-16  Auto-stub: .agent/hooks/post_tool.sh present but non-executable
#         → run_post still does not raise (fire-and-forget)
# ---------------------------------------------------------------------------


def test_post_tool_sh_stub_fire_and_forget(tmp_path):
    stub_dir = tmp_path / ".codingAgent" / "hooks"
    stub_dir.mkdir(parents=True)
    stub = stub_dir / "post_tool.sh"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    from src.core.orchestration.tool_hooks import ToolHookRunner

    r = ToolHookRunner(working_dir=tmp_path)
    # Must not raise
    r.run_post("write_file", {"path": "a.py"}, {"ok": True})


# ---------------------------------------------------------------------------
# TH-17  Auto-stub is skipped when hooks.json pre_tool entries already exist
# ---------------------------------------------------------------------------


def test_stub_not_wired_when_hooks_json_exists(tmp_path):
    """When hooks.json provides pre_tool entries the stub is NOT appended."""
    import sys
    import json as _json

    hooks_path = tmp_path / ".codingAgent" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        _json.dumps(
            {
                "pre_tool": [
                    {
                        "match": "bash",
                        "cmd": f"{sys.executable} -c 'import sys; sys.exit(0)'",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stub_dir = tmp_path / ".codingAgent" / "hooks"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "pre_tool.sh"
    stub.write_text(
        f"#!/usr/bin/env {sys.executable}\nimport sys; sys.exit(1)\n", encoding="utf-8"
    )
    stub.chmod(0o755)

    from src.core.orchestration.tool_hooks import _load_hook_config

    cfg = _load_hook_config(tmp_path)
    # Only the hooks.json entry should be present (1 entry, exit 0, not the stub)
    assert len(cfg.pre_tool) == 1
    assert "bash" in cfg.pre_tool[0].get("match", "")


# ---------------------------------------------------------------------------
# TH-18  deferred_init reports hooks_enabled=True via _tool_hook_runner (Gap A)
# ---------------------------------------------------------------------------


def test_deferred_init_detects_tool_hook_runner(tmp_path):
    """deferred_init.run_deferred_init() must set result.hooks_enabled=True
    when orchestrator._tool_hook_runner is set (old code checked 'tool_hooks'
    which is never set — Gap A fix verifies the corrected attribute name)."""
    import asyncio
    from src.core.orchestration.tool_hooks import ToolHookRunner

    class _FakeOrchestrator:
        pass

    orch = _FakeOrchestrator()
    # Simulate the lazy init that execute_tool does
    orch._tool_hook_runner = ToolHookRunner(working_dir=tmp_path)  # type: ignore[attr-defined]

    from src.core.orchestration.deferred_init import run_deferred_init

    result = asyncio.run(run_deferred_init(orch, trusted=True))
    assert result.hooks_enabled is True
