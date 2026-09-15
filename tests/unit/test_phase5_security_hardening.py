"""Phase-5 security-hardening tests (audit HS-1).

Covers HS-1: autonomous mode must NOT silently suppress approval prompts.
Previously ``is_autonomous()`` auto-allowed every DANGER/PROMPT tool with no
operator override (any process with env control disabled all safety prompts).
Now autonomous mode only skips the approval prompt for tools the operator
explicitly opted into via ``set_autonomous_approve()`` /
``configure(autonomous_approve=...)`` or the ``CODINGAGENT_AUTONOMOUS_APPROVE``
env var; every other gated tool is DENIED loudly (fail-closed), never
auto-approved.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestration import tool_execution_pipeline as pipe


@pytest.fixture(autouse=True)
def _restore_allowlist():
    from src.tools.tools_config import reset_to_defaults

    yield
    reset_to_defaults()


def _make_orch(**overrides):
    orch = MagicMock()
    orch.working_dir = "/tmp/proj"
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
# HS-1 — autonomous_approval_allowed (tools_config)
# ---------------------------------------------------------------------------


def test_autonomous_approval_fails_closed_by_default():
    from src.tools.tools_config import autonomous_approval_allowed

    assert autonomous_approval_allowed("bash") is False
    assert autonomous_approval_allowed("delete_file") is False
    assert autonomous_approval_allowed("run") is False
    assert autonomous_approval_allowed("read_file") is False


def test_autonomous_approval_allowlist_honors_canonical_name_and_alias():
    from src.tools.tools_config import set_autonomous_approve

    set_autonomous_approve({"bash"})
    from src.tools.tools_config import autonomous_approval_allowed

    assert autonomous_approval_allowed("bash") is True
    assert autonomous_approval_allowed("run") is True  # alias resolves to bash
    assert autonomous_approval_allowed("shell") is True
    assert autonomous_approval_allowed("delete_file") is False


def test_autonomous_approval_wildcard_allows_all_gated_tools():
    from src.tools.tools_config import (
        autonomous_approval_allowed,
        set_autonomous_approve,
    )

    set_autonomous_approve({"*"})
    assert autonomous_approval_allowed("bash") is True
    assert autonomous_approval_allowed("delete_file") is True
    assert autonomous_approval_allowed("delegate_task") is True


def test_autonomous_approval_env_var_override(monkeypatch):
    from src.tools.tools_config import autonomous_approval_allowed

    monkeypatch.setenv("CODINGAGENT_AUTONOMOUS_APPROVE", "bash, delete_file")
    assert autonomous_approval_allowed("bash") is True
    assert autonomous_approval_allowed("run") is True
    assert autonomous_approval_allowed("delete_file") is True
    assert autonomous_approval_allowed("delegate_task") is False

    monkeypatch.setenv("CODINGAGENT_AUTONOMOUS_APPROVE", "*")
    assert autonomous_approval_allowed("delegate_task") is True


def test_autonomous_approve_roundtrip_and_reset():
    from src.tools.tools_config import (
        get_autonomous_approve,
        reset_to_defaults,
        set_autonomous_approve,
    )

    assert get_autonomous_approve() == frozenset()
    set_autonomous_approve({"bash", "run_tests"})
    assert get_autonomous_approve() == frozenset({"bash", "run_tests"})
    reset_to_defaults()
    assert get_autonomous_approve() == frozenset()


def test_configure_accepts_autonomous_approve_option():
    from src.tools.tools_config import (
        autonomous_approval_allowed,
        configure,
    )

    configure(autonomous_approve={"*"})
    assert autonomous_approval_allowed("bash") is True


def test_configure_with_none_leaves_allowlist_unchanged():
    from src.tools.tools_config import (
        autonomous_approval_allowed,
        configure,
        set_autonomous_approve,
    )

    set_autonomous_approve({"bash"})
    configure(autonomous_approve=None)
    assert autonomous_approval_allowed("bash") is True


# ---------------------------------------------------------------------------
# HS-1 — tool_execution_pipeline._run_permission_gate (production path)
# ---------------------------------------------------------------------------


def test_pipeline_denies_gated_tool_in_autonomous_mode_without_allowlist():
    with patch("src.tools.tools_config.is_autonomous", return_value=True):
        result = pipe._run_permission_gate(
            _make_orch(), "bash", {"command": "ls"}, "id-1", needs_gate=True
        )

    assert result == {
        "ok": False,
        "error": (
            "Tool 'bash' requires approval and is not in the autonomous "
            "approval allowlist. Add it via set_autonomous_approve() or the "
            "CODINGAGENT_AUTONOMOUS_APPROVE env var to auto-approve it."
        ),
    }


def test_pipeline_auto_approves_allowlisted_tool_in_autonomous_mode():
    from src.tools.tools_config import set_autonomous_approve

    set_autonomous_approve({"bash"})
    with patch("src.tools.tools_config.is_autonomous", return_value=True):
        result = pipe._run_permission_gate(
            _make_orch(), "bash", {"command": "ls"}, "id-2", needs_gate=True
        )

    assert result is None


def test_pipeline_denies_alias_of_gated_tool_in_autonomous_mode():
    with patch("src.tools.tools_config.is_autonomous", return_value=True):
        result = pipe._run_permission_gate(
            _make_orch(), "run", {"command": "ls"}, "id-3", needs_gate=True
        )

    assert result is not None
    assert result["ok"] is False


def test_pipeline_returns_none_when_no_gate_needed_even_in_autonomous_mode():
    with patch("src.tools.tools_config.is_autonomous", return_value=True):
        result = pipe._run_permission_gate(
            _make_orch(), "read_file", {"path": "x"}, "id-4", needs_gate=False
        )

    assert result is None


def test_pipeline_proceeds_through_approval_in_interactive_mode():
    """Non-autonomous gated tool still registers a gate and waits (granted
    here via a pre-armed approval event)."""

    class _GrantedEvent:
        def wait(self, timeout=None):
            return True

    with (
        patch("src.tools.tools_config.is_autonomous", return_value=False),
        patch.object(pipe, "register_tool_gate", return_value=_GrantedEvent()),
        patch.object(pipe, "is_tool_denied", return_value=False),
        patch.object(pipe, "discard_tool_denied", return_value=None),
    ):
        result = pipe._run_permission_gate(
            _make_orch(), "bash", {"command": "ls"}, "id-5", needs_gate=True
        )

    assert result is None


# ---------------------------------------------------------------------------
# HS-1 — PermissionGateway._gate5_user_approval (kept consistent)
# ---------------------------------------------------------------------------


def test_gateway_denies_gated_tool_in_autonomous_mode_without_allowlist():
    import src.core.orchestration.permission_gateway as pg

    with patch.object(pg, "_is_autonomous", return_value=True):
        result = pg.PermissionGateway(_make_orch())._gate5_user_approval(
            "bash", {"command": "ls"}
        )

    assert result.allowed is False
    assert result.gate == 5
    assert "allowlist" in result.rejection["error"]


def test_gateway_auto_approves_allowlisted_tool_in_autonomous_mode():
    import src.core.orchestration.permission_gateway as pg
    from src.tools.tools_config import set_autonomous_approve

    set_autonomous_approve({"bash"})
    with patch.object(pg, "_is_autonomous", return_value=True):
        result = pg.PermissionGateway(_make_orch())._gate5_user_approval(
            "bash", {"command": "ls"}
        )

    assert result.allowed is True


# ---------------------------------------------------------------------------
# HS-1 — _bash_exec._check_tier3_approval (bash command-level tier-3 gate)
# ---------------------------------------------------------------------------


def test_bash_tier3_no_longer_suppressed_by_autonomous_alone():
    """Autonomous mode without the allowlist must NOT skip the tier-3 prompt."""
    import src.tools._bash_exec as bx

    with (
        patch.object(bx, "_is_autonomous", return_value=True),
        patch("src.tools.tools_config.autonomous_approval_allowed", return_value=False),
        patch.object(bx, "register_bash_gate", side_effect=AssertionError),
    ):
        # Approval gate is reached: register_bash_gate would be called, so if
        # the gate were suppressed the assertion would fire.
        with pytest.raises(AssertionError):
            bx._check_tier3_approval("rm -rf /tmp/x")


def test_bash_tier3_auto_approved_when_allowlisted():
    import src.tools._bash_exec as bx
    from src.tools.tools_config import set_autonomous_approve

    set_autonomous_approve({"bash"})
    with patch.object(bx, "_is_autonomous", return_value=True):
        assert bx._check_tier3_approval("rm -rf /tmp/x") is None


def test_bash_tier3_prompts_when_not_autonomous():
    import src.tools._bash_exec as bx

    with (
        patch.object(bx, "_is_autonomous", return_value=False),
        patch.object(
            bx, "register_bash_gate", return_value=type("E", (), {"wait": lambda self, **k: False})()
        ),
        patch.object(bx, "is_bash_denied", return_value=True),
        patch.object(bx, "discard_bash_denied", return_value=None),
    ):
        result = bx._check_tier3_approval("rm -rf /tmp/x")

    assert result is not None
    assert result["status"] == "error"


def test_bash_allowlist_uses_canonical_bash_name_not_command():
    """The tier-3 bash gate must consult the allowlist for the *tool* name
    ("bash"), not the command string being executed."""
    import src.tools._bash_exec as bx

    with (
        patch.object(bx, "_is_autonomous", return_value=True),
        patch("src.tools.tools_config.autonomous_approval_allowed", return_value=True) as m,
    ):
        assert bx._check_tier3_approval("rm -rf /tmp/x") is None
        m.assert_called_once_with("bash")


# ---------------------------------------------------------------------------
# HS-1 — ToolExecutionService._check_permission_gate (kept consistent)
# ---------------------------------------------------------------------------


def test_service_denies_gated_tool_in_autonomous_mode_without_allowlist():
    import asyncio

    from src.core.orchestration.tool_execution_service import ToolExecutionService

    with (
        patch("src.tools.tools_config.get_tool_permission") as mock_perm,
        patch("src.tools.tools_config.is_autonomous", return_value=True),
    ):
        from src.tools.tools_config import PermissionLevel

        mock_perm.return_value = PermissionLevel.DANGER
        svc = ToolExecutionService(registry=MagicMock(), event_bus=MagicMock())
        verdict = asyncio.run(svc.pre_execute("bash", {"command": "ls"}))

    assert verdict.blocked is True
    assert "allowlist" in verdict.result["error"]


def test_service_auto_approves_allowlisted_gated_tool():
    import asyncio

    from src.core.orchestration.tool_execution_service import ToolExecutionService
    from src.tools.tools_config import set_autonomous_approve

    set_autonomous_approve({"bash"})
    with (
        patch("src.tools.tools_config.get_tool_permission") as mock_perm,
        patch("src.tools.tools_config.is_autonomous", return_value=True),
    ):
        from src.tools.tools_config import PermissionLevel

        mock_perm.return_value = PermissionLevel.DANGER
        svc = ToolExecutionService(registry=MagicMock(), event_bus=MagicMock())
        verdict = asyncio.run(svc.pre_execute("bash", {"command": "ls"}))

    assert verdict.blocked is False