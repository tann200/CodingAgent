"""Phase-2 security-hardening tests (audit 2.1 / 2.2 / 2.8).

Covers:
- 2.2 Alias evasion of permission rules: tool aliases (run/shell/cmd -> bash,
  ls -> list_files, write -> write_file, ...) are normalized to their canonical
  name BEFORE any permission/policy check, so a deny/approval rule keyed on the
  canonical name cannot be bypassed via an alias.
- 2.8 delete_file auto-approval: deletion is irreversible and must never be
  auto-approved merely because the target is inside the working directory.
- 2.1 Sandbox default-strict in autonomous mode: refusing to fall back to
  unsandboxed execution when no sandbox backend is available/enforcing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.tools.sandbox as sbox
from src.core.orchestration import tool_execution_pipeline as pipe
from src.tools.tools_config import get_tool_permission, resolve_tool_alias


def _make_orch(**overrides):
    orch = MagicMock()
    orch.working_dir = None
    orch._session_read_files = set()
    orch._session_modified_files = set()
    orch._dry_run = False
    orch.event_bus = MagicMock()
    orch.plan_mode = None
    orch._plan_mode_approved = None
    orch.rollback_manager = MagicMock()
    orch.current_role = None
    orch.explore_mode = False
    orch.cost_tracker = MagicMock()
    orch.session_store = MagicMock()
    for k, v in overrides.items():
        setattr(orch, k, v)
    return orch


# ---------------------------------------------------------------------------
# 2.2 — Alias resolution / permission normalization
# ---------------------------------------------------------------------------


def test_resolve_tool_alias_maps_to_canonical_name():
    assert resolve_tool_alias("run") == "bash"
    assert resolve_tool_alias("shell") == "bash"
    assert resolve_tool_alias("cmd") == "bash"
    assert resolve_tool_alias("ls") == "list_files"
    assert resolve_tool_alias("write") == "write_file"
    assert resolve_tool_alias("edit") == "edit_file_atomic"


def test_resolve_tool_alias_returns_unknown_name_unchanged():
    assert resolve_tool_alias("bash") == "bash"
    assert resolve_tool_alias("read_file") == "read_file"
    assert resolve_tool_alias("not_a_tool") == "not_a_tool"


def test_bash_alias_normalizes_to_danger_permission_level():
    # "run" must be normalized to "bash" (DANGER), not left at the default
    # WORKSPACE_WRITE — otherwise DANGER tools skip the approval gate.
    from src.tools.tools_config import PermissionLevel

    assert get_tool_permission("bash") == PermissionLevel.DANGER
    assert get_tool_permission(resolve_tool_alias("run")) == PermissionLevel.DANGER


def test_execute_tool_impl_normalizes_alias_before_guards():
    """execute_tool_impl must pass the canonical name to downstream guards when
    invoked with an alias."""

    captured = {}

    def _spy_read_guard(orch, name, args, write_tools):
        captured["name"] = name
        return None

    def _run(_name):
        captured.clear()
        with (
            patch.object(pipe, "_check_read_before_write", side_effect=_spy_read_guard),
            patch.object(pipe, "_check_workspace_scope_guard", return_value=None),
            patch.object(pipe, "_check_plan_mode_guard", return_value=None),
            patch.object(pipe, "_check_explore_mode_guard", return_value=None),
            patch.object(pipe, "_check_permission_mode_guard", return_value=None),
            patch.object(pipe, "_run_preflight_and_lookup", return_value=(None, None)),
            patch.object(pipe, "_run_permission_gate", return_value=None),
            patch.object(pipe, "_run_sandbox_and_snapshot", return_value=None),
            patch.object(pipe, "_dispatch_tool_call", return_value={"ok": True}),
            patch.object(pipe, "_run_post_execution", return_value={"ok": True}),
        ):
            orch = _make_orch(_dry_run=False)
            pipe.execute_tool_impl(orch, {"name": _name, "arguments": {"path": "x"}})

    # Alias "run" must reach the guards as canonical "bash".
    _run("run")
    assert captured["name"] == "bash"
    # Alias "write" must reach the guards as canonical "write_file".
    _run("write")
    assert captured["name"] == "write_file"
    # Canonical name passes through unchanged.
    _run("bash")
    assert captured["name"] == "bash"


# ---------------------------------------------------------------------------
# 2.8 — delete_file must never be auto-approved
# ---------------------------------------------------------------------------


def test_delete_file_not_in_workdir_safe_tools():
    import src.core.orchestration.permission_gateway as pg

    assert "delete_file" not in pg._WORKDIR_SAFE_TOOLS


def test_pipeline_requires_approval_for_delete_file_inside_workdir():
    """delete_file targeting a file inside the workdir must STILL require
    explicit approval (deletion is irreversible)."""
    orch = _make_orch(working_dir="/tmp/proj")
    assert (
        pipe._check_workdir_confinement(orch, "delete_file", {"path": "file.py"})
        is True
    )
    assert (
        pipe._check_workdir_confinement(
            orch, "delete_file", {"path": "/tmp/proj/file.py"}
        )
        is True
    )


def test_permission_gateway_never_confines_delete_file():
    import src.core.orchestration.permission_gateway as pg

    assert (
        pg._is_workdir_confined(
            "delete_file", {"path": "/tmp/proj/file.py"}, "/tmp/proj"
        )
        is False
    )


# ---------------------------------------------------------------------------
# 2.1 — Sandbox default-strict in autonomous mode
# ---------------------------------------------------------------------------


def test_enforcement_required_when_env_flag_set():
    with (
        patch.object(sbox, "_REQUIRE_ENFORCEMENT", True),
        patch.object(sbox, "_autonomous_mode", return_value=False),
    ):
        assert sbox._enforcement_required() is True


def test_enforcement_required_when_autonomous_mode():
    with (
        patch.object(sbox, "_REQUIRE_ENFORCEMENT", False),
        patch.object(sbox, "_autonomous_mode", return_value=True),
    ):
        assert sbox._enforcement_required() is True


def test_enforcement_not_required_when_interactive():
    with (
        patch.object(sbox, "_REQUIRE_ENFORCEMENT", False),
        patch.object(sbox, "_autonomous_mode", return_value=False),
    ):
        assert sbox._enforcement_required() is False


def test_run_sandboxed_refuses_unsandboxed_in_autonomous_mode():
    """With no bwrap/sandbox-exec available and enforcement required (autonomous),
    run_sandboxed must raise rather than silently run with full privileges."""
    with (
        patch.object(sbox, "_bwrap_available", return_value=False),
        patch.object(sbox, "_sandbox_exec_available", return_value=False),
        patch.object(sbox, "_REQUIRE_ENFORCEMENT", False),
        patch.object(sbox, "_autonomous_mode", return_value=True),
    ):
        with pytest.raises(RuntimeError):
            sbox.run_sandboxed(["echo", "hi"], cwd=Path("/tmp"))


def test_run_sandboxed_falls_back_when_interactive():
    """In interactive (non-autonomous) mode with enforcement not required, the
    documented fallback to plain subprocess is preserved (no regression)."""
    with (
        patch.object(sbox, "_bwrap_available", return_value=False),
        patch.object(sbox, "_sandbox_exec_available", return_value=False),
        patch.object(sbox, "_REQUIRE_ENFORCEMENT", False),
        patch.object(sbox, "_autonomous_mode", return_value=False),
        patch("src.tools.sandbox.subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        result = sbox.run_sandboxed(["echo", "hi"], cwd=Path("/tmp"))
        assert result.returncode == 0


def test_autonomous_mode_helper_respects_env():
    from src.tools import tools_config

    # is_autonomous() is driven by the CODINGAGENT_AUTONOMOUS env var.
    with patch.object(tools_config, "is_autonomous", return_value=True):
        assert sbox._autonomous_mode() is True
