"""Post-audit defect batch: permissions.json enforcement at the main tool gate.

The TUI's "Allow Always" button promised a persisted grant but the production
pipeline never honoured it, twice over:

1. ``PermissionPolicy.add_rule`` did not exist, so the TUI's persist block
   raised ``AttributeError`` (silently logged) and the rule never reached
   permissions.json.
2. ``tool_execution_pipeline`` read ``getattr(orch, "permission_policy")`` —
   an attribute the orchestrator never assigns — so even a written rule was
   never consulted at the gate (only the doom-loop guard read the policy).

This file pins the fixed behaviour end to end:

- global DENY rules deny at the gate (with and without an active agent),
- PERM-W4 merge: agent rules append after global rules (last-match-wins),
- explicit ASK rules force the prompt in interactive mode and fail closed
  in autonomous mode (HS-1 still applies),
- explicit ALLOW rules skip the prompt *interactive only* — autonomous mode
  still requires the HS-1 allowlist, so permissions.json is not an HS-1
  bypass — and ``delete_file`` is never auto-suppressed (HS-5/SEC-8).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestration import tool_execution_pipeline as pipe
from src.core.orchestration.permission_policy import (
    Behavior,
    PermissionPolicy,
    PermissionRule,
    reset_permission_policy,
)


@pytest.fixture(autouse=True)
def _isolated_policy():
    # Empty hermetic policy: no rule matches anything, so behaviour is
    # identical to "no permissions.json" regardless of the host machine.
    reset_permission_policy(PermissionPolicy(rules=[]))
    yield
    reset_permission_policy(None)


def _policy(*rules: PermissionRule) -> PermissionPolicy:
    return PermissionPolicy(rules=list(rules))


def _deny(pattern: str) -> PermissionRule:
    return PermissionRule(pattern=pattern, behavior=Behavior.DENY)


def _allow(pattern: str) -> PermissionRule:
    return PermissionRule(pattern=pattern, behavior=Behavior.ALLOW)


def _ask(pattern: str) -> PermissionRule:
    return PermissionRule(pattern=pattern, behavior=Behavior.ASK)


def _make_orch(**overrides):
    # Explicit attributes, not auto-Mocks: MagicMock would fabricate a
    # truthy permission_policy / active_agent and defeat the None fallbacks.
    orch = MagicMock()
    orch.working_dir = "/tmp/proj"
    orch.event_bus = MagicMock()
    orch.permission_policy = None
    orch.active_agent = None
    for k, v in overrides.items():
        setattr(orch, k, v)
    return orch


class _GrantedEvent:
    def wait(self, timeout=None):
        return True


# ---------------------------------------------------------------------------
# _check_agent_permission_override — global DENY rules reach the gate
# ---------------------------------------------------------------------------


class TestGlobalDenyAtOverride:
    def test_global_deny_denies_without_active_agent(self, tmp_path):
        reset_permission_policy(_policy(_deny("rm_*")))
        orch = _make_orch(active_agent=None, working_dir=str(tmp_path))

        result = pipe._check_agent_permission_override(orch, "rm_file", {})

        assert result is not None and result["ok"] is False
        assert "denied by permission rules" in result["error"]

    def test_global_deny_denies_with_agent_without_rules(self, tmp_path):
        reset_permission_policy(_policy(_deny("git_push")))

        class _Agent:
            permission_rules = []

            def get_merged_policy(self, base_policy=None):
                return base_policy

        orch = _make_orch(active_agent=_Agent(), working_dir=str(tmp_path))

        result = pipe._check_agent_permission_override(orch, "git_push", {})

        assert result is not None and result["ok"] is False

    def test_agent_allow_rule_overrides_global_deny(self, tmp_path):
        """PERM-W4: agent rules append after global rules, last-match-wins."""
        from src.core.orchestration.agent_types import AgentDefinition

        reset_permission_policy(_policy(_deny("*")))
        agent = AgentDefinition(
            id="ops",
            name="ops",
            description="test agent",
            permission_rules=[{"pattern": "edit_file", "behavior": "allow"}],
        )
        orch = _make_orch(active_agent=agent, working_dir=str(tmp_path))

        assert pipe._check_agent_permission_override(orch, "edit_file", {}) is None
        assert (
            pipe._check_agent_permission_override(orch, "write_file", {}) is not None
        )

    def test_no_matching_rule_stays_permissive(self, tmp_path):
        orch = _make_orch(active_agent=None, working_dir=str(tmp_path))

        assert pipe._check_agent_permission_override(orch, "bash", {}) is None


# ---------------------------------------------------------------------------
# _run_permission_gate — DENY / ASK / ALLOW reconciliation
# ---------------------------------------------------------------------------


class TestGateDenyRule:
    def test_deny_rule_hard_denies_even_when_no_gate_needed(self):
        reset_permission_policy(_policy(_deny("bash")))

        result = pipe._run_permission_gate(
            _make_orch(), "bash", {"command": "ls"}, "id-1", needs_gate=False
        )

        assert result is not None and result["ok"] is False
        assert "permissions.json rule" in result["error"]

    def test_deny_rule_wins_in_autonomous_mode(self):
        reset_permission_policy(_policy(_deny("bash")))

        with patch("src.tools.tools_config.is_autonomous", return_value=True):
            result = pipe._run_permission_gate(
                _make_orch(), "bash", {"command": "ls"}, "id-2", needs_gate=True
            )

        assert result is not None and result["ok"] is False


class TestGateAskRule:
    def test_ask_rule_forces_prompt_when_tool_otherwise_ungated(self):
        reset_permission_policy(_policy(_ask("read_file")))

        with (
            patch("src.tools.tools_config.is_autonomous", return_value=False),
            patch.object(
                pipe, "register_tool_gate", return_value=_GrantedEvent()
            ) as reg_gate,
            patch.object(pipe, "is_tool_denied", return_value=False),
            patch.object(pipe, "discard_tool_denied", return_value=None),
        ):
            result = pipe._run_permission_gate(
                _make_orch(), "read_file", {"path": "x"}, "id-3", needs_gate=False
            )

        assert result is None
        reg_gate.assert_called_once()

    def test_ask_rule_fails_closed_in_autonomous_mode(self):
        reset_permission_policy(_policy(_ask("read_file")))

        with patch("src.tools.tools_config.is_autonomous", return_value=True):
            result = pipe._run_permission_gate(
                _make_orch(), "read_file", {"path": "x"}, "id-4", needs_gate=False
            )

        assert result is not None and result["ok"] is False


class TestGateAllowRule:
    def test_allow_rule_skips_prompt_interactively(self):
        """The TUI 'Allow Always' promise: no re-prompt on later calls."""
        reset_permission_policy(_policy(_allow("git_commit")))

        with (
            patch("src.tools.tools_config.is_autonomous", return_value=False),
            patch.object(pipe, "register_tool_gate") as reg,
        ):
            result = pipe._run_permission_gate(
                _make_orch(), "git_commit", {}, "id-5", needs_gate=True
            )

        assert result is None
        reg.assert_not_called()

    def test_allow_rule_does_not_bypass_hs1_autonomous_allowlist(self):
        """permissions.json must NOT become a second autonomous bypass (HS-1)."""
        reset_permission_policy(_policy(_allow("bash")))

        with patch("src.tools.tools_config.is_autonomous", return_value=True):
            result = pipe._run_permission_gate(
                _make_orch(), "bash", {"command": "ls"}, "id-6", needs_gate=True
            )

        assert result is not None and result["ok"] is False
        assert "autonomous" in result["error"]

    def test_allow_rule_never_suppresses_delete_file(self):
        """HS-5/SEC-8: delete_file must never be auto-approved (HS-5)."""
        reset_permission_policy(_policy(_allow("*")))

        with (
            patch("src.tools.tools_config.is_autonomous", return_value=False),
            patch.object(
                pipe, "register_tool_gate", return_value=_GrantedEvent()
            ) as reg_gate,
            patch.object(pipe, "is_tool_denied", return_value=False),
            patch.object(pipe, "discard_tool_denied", return_value=None),
        ):
            result = pipe._run_permission_gate(
                _make_orch(),
                "delete_file",
                {"path": "x.txt"},
                "id-7",
                needs_gate=True,
            )

        assert result is None  # prompt was shown and granted, not skipped
        reg_gate.assert_called_once()

    def test_unmatched_tool_keeps_existing_gate_behaviour(self):
        # Empty policy (fixture): needs_gate=True still prompts (granted here).
        with (
            patch("src.tools.tools_config.is_autonomous", return_value=False),
            patch.object(pipe, "register_tool_gate", return_value=_GrantedEvent()),
            patch.object(pipe, "is_tool_denied", return_value=False),
            patch.object(pipe, "discard_tool_denied", return_value=None),
        ):
            result = pipe._run_permission_gate(
                _make_orch(), "bash", {"command": "ls"}, "id-8", needs_gate=True
            )

        assert result is None
