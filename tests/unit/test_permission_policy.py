"""
tests/unit/test_permission_policy.py — Unit tests for Sprint A-4: PermissionPolicy.
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import tempfile
from pathlib import Path


from src.core.orchestration.permission_policy import (
    DOOM_LOOP_TOKEN,
    Behavior,
    PermissionPolicy,
    PermissionRule,
    make_default_policy,
    make_readonly_policy,
    get_permission_policy,
    reset_permission_policy,
)


# ---------------------------------------------------------------------------
# Behavior enum
# ---------------------------------------------------------------------------


class TestBehavior:
    def test_string_values(self):
        assert Behavior.ALLOW == "allow"
        assert Behavior.DENY == "deny"
        assert Behavior.ASK == "ask"

    def test_case_insensitive_construction(self):
        assert Behavior("ALLOW") == Behavior.ALLOW
        assert Behavior("Deny") == Behavior.DENY
        assert Behavior("ASK") == Behavior.ASK


# ---------------------------------------------------------------------------
# PermissionRule
# ---------------------------------------------------------------------------


class TestPermissionRule:
    def test_exact_match(self):
        rule = PermissionRule(pattern="bash", behavior=Behavior.DENY)
        assert rule.matches("bash") is True
        assert rule.matches("bash_readonly") is False

    def test_wildcard_prefix(self):
        rule = PermissionRule(pattern="write_*", behavior=Behavior.ASK)
        assert rule.matches("write_file") is True
        assert rule.matches("write_anything") is True
        assert rule.matches("read_file") is False

    def test_glob_star_matches_all(self):
        rule = PermissionRule(pattern="*", behavior=Behavior.ALLOW)
        assert rule.matches("any_tool") is True
        assert rule.matches("") is True

    def test_case_insensitive_matching(self):
        rule = PermissionRule(pattern="BASH", behavior=Behavior.DENY)
        assert rule.matches("bash") is True
        assert rule.matches("BASH") is True
        assert rule.matches("Bash") is True

    def test_roundtrip(self):
        rule = PermissionRule(pattern="write_*", behavior=Behavior.ASK)
        d = rule.to_dict()
        assert d == {"pattern": "write_*", "behavior": "ask"}
        restored = PermissionRule.from_dict(d)  # type: ignore[arg-type]
        assert restored.pattern == rule.pattern
        assert restored.behavior == rule.behavior

    def test_invalid_behavior_defaults_to_allow(self):
        """Invalid behavior value in from_dict should log a warning and default to ALLOW."""
        rule = PermissionRule.from_dict({"pattern": "bash", "behavior": "INVALID"})  # type: ignore[arg-type]
        assert rule.behavior == Behavior.ALLOW
        assert rule.pattern == "bash"


# ---------------------------------------------------------------------------
# PermissionPolicy — core evaluation
# ---------------------------------------------------------------------------


class TestPermissionPolicyEvaluation:
    def test_empty_policy_allows_everything(self):
        policy = PermissionPolicy()
        assert policy.check("any_tool") == Behavior.ALLOW
        assert policy.is_allowed("any_tool") is True
        assert policy.is_denied("any_tool") is False

    def test_single_deny_rule(self):
        policy = PermissionPolicy(
            [PermissionRule(pattern="bash", behavior=Behavior.DENY)]
        )
        assert policy.check("bash") == Behavior.DENY
        assert policy.is_denied("bash") is True
        assert policy.check("read_file") == Behavior.ALLOW

    def test_last_matching_wins(self):
        """Later rules override earlier ones — last-matching-wins."""
        policy = PermissionPolicy(
            [
                PermissionRule(pattern="*", behavior=Behavior.DENY),  # deny all
                PermissionRule(
                    pattern="read_*", behavior=Behavior.ALLOW
                ),  # but allow reads
            ]
        )
        assert policy.check("read_file") == Behavior.ALLOW  # overridden to allow
        assert policy.check("write_file") == Behavior.DENY  # still denied

    def test_specific_rule_overrides_broad_rule(self):
        """A more-specific later rule overrides a broad earlier rule."""
        policy = PermissionPolicy(
            [
                PermissionRule(pattern="read_*", behavior=Behavior.ALLOW),
                PermissionRule(
                    pattern="read_file", behavior=Behavior.ASK
                ),  # specific override
            ]
        )
        assert policy.check("read_file") == Behavior.ASK
        assert policy.check("read_anything") == Behavior.ALLOW

    def test_ask_behavior(self):
        policy = PermissionPolicy(
            [PermissionRule(pattern="bash", behavior=Behavior.ASK)]
        )
        assert policy.check("bash") == Behavior.ASK
        assert policy.requires_confirmation("bash") is True
        assert policy.is_denied("bash") is False
        assert policy.is_allowed("bash") is True  # ASK is still allowed

    def test_default_behavior_deny(self):
        """Policy with default=deny blocks everything not explicitly allowed."""
        policy = PermissionPolicy(
            [PermissionRule(pattern="read_*", behavior=Behavior.ALLOW)],
            default_behavior=Behavior.DENY,
        )
        assert policy.check("read_file") == Behavior.ALLOW
        assert policy.check("write_file") == Behavior.DENY  # default
        assert policy.check("bash") == Behavior.DENY  # default

    def test_len(self):
        policy = PermissionPolicy(
            [
                PermissionRule(pattern="bash", behavior=Behavior.DENY),
                PermissionRule(pattern="write_*", behavior=Behavior.ASK),
            ]
        )
        assert len(policy) == 2

    def test_iteration(self):
        rules = [
            PermissionRule(pattern="bash", behavior=Behavior.DENY),
            PermissionRule(pattern="write_*", behavior=Behavior.ASK),
        ]
        policy = PermissionPolicy(rules)
        listed = list(policy)
        assert listed == rules


# ---------------------------------------------------------------------------
# Doom-loop integration
# ---------------------------------------------------------------------------


class TestDoomLoop:
    def test_doom_loop_denied_by_default_when_no_rule(self):
        """check_doom_loop() returns DENY even when no explicit rule exists."""
        policy = PermissionPolicy()  # no rules
        assert policy.check_doom_loop() == Behavior.DENY

    def test_doom_loop_explicit_deny(self):
        policy = PermissionPolicy(
            [PermissionRule(pattern=DOOM_LOOP_TOKEN, behavior=Behavior.DENY)]
        )
        assert policy.check_doom_loop() == Behavior.DENY

    def test_doom_loop_explicit_allow(self):
        """An explicit allow rule for doom_loop respects user's choice."""
        policy = PermissionPolicy(
            [PermissionRule(pattern=DOOM_LOOP_TOKEN, behavior=Behavior.ALLOW)]
        )
        assert policy.check_doom_loop() == Behavior.ALLOW

    def test_doom_loop_token_constant(self):
        assert DOOM_LOOP_TOKEN == "doom_loop"


# ---------------------------------------------------------------------------
# combined_check (integration with ToolPermissionContext)
# ---------------------------------------------------------------------------


class TestCombinedCheck:
    def test_combined_uses_policy_allow(self):
        policy = PermissionPolicy()
        assert policy.combined_check("read_file") == Behavior.ALLOW

    def test_combined_policy_deny_overrides_cli_allow(self):
        policy = PermissionPolicy(
            [PermissionRule(pattern="bash", behavior=Behavior.DENY)]
        )
        assert policy.combined_check("bash") == Behavior.DENY

    def test_combined_cli_deny_overrides_policy_allow(self):
        """If CLI context blocks the tool, combined_check returns DENY."""
        policy = PermissionPolicy()  # allows everything

        class FakeCLI:
            def blocks(self, tool_name: str) -> bool:
                return tool_name == "bash"

        assert policy.combined_check("bash", FakeCLI()) == Behavior.DENY
        assert policy.combined_check("read_file", FakeCLI()) == Behavior.ALLOW

    def test_combined_none_cli_context(self):
        policy = PermissionPolicy(
            [PermissionRule(pattern="bash", behavior=Behavior.DENY)]
        )
        assert policy.combined_check("bash", None) == Behavior.DENY
        assert policy.combined_check("read_file", None) == Behavior.ALLOW


# ---------------------------------------------------------------------------
# Serialisation / persistence
# ---------------------------------------------------------------------------


class TestPermissionPolicySerialization:
    def test_to_dict_roundtrip(self):
        original = PermissionPolicy(
            [
                PermissionRule(pattern="*", behavior=Behavior.ALLOW),
                PermissionRule(pattern="bash", behavior=Behavior.DENY),
            ],
            default_behavior=Behavior.ALLOW,
        )
        d = original.to_dict()
        restored = PermissionPolicy.from_dict(d)

        assert len(restored) == len(original)
        assert restored.check("bash") == Behavior.DENY
        assert restored.check("read_file") == Behavior.ALLOW

    def test_load_from_dict_format(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "default_behavior": "allow",
                    "rules": [
                        {"pattern": "write_*", "behavior": "ask"},
                        {"pattern": "doom_loop", "behavior": "deny"},
                    ],
                },
                f,
            )
            path = Path(f.name)
        try:
            policy = PermissionPolicy.load(path)
            assert policy.check("write_file") == Behavior.ASK
            assert policy.check("doom_loop") == Behavior.DENY
            assert policy.check("read_file") == Behavior.ALLOW
        finally:
            path.unlink(missing_ok=True)

    def test_load_from_bare_array_format(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {"pattern": "bash", "behavior": "deny"},
                    {"pattern": "read_*", "behavior": "allow"},
                ],
                f,
            )
            path = Path(f.name)
        try:
            policy = PermissionPolicy.load(path)
            assert policy.check("bash") == Behavior.DENY
            assert policy.check("read_file") == Behavior.ALLOW
        finally:
            path.unlink(missing_ok=True)

    def test_load_missing_file_returns_empty(self):
        policy = PermissionPolicy.load(Path("/nonexistent/permissions.json"))
        # An empty policy should allow everything (default)
        assert policy.check("anything") == Behavior.ALLOW

    def test_save_and_reload(self):
        original = PermissionPolicy(
            [
                PermissionRule(pattern="write_*", behavior=Behavior.ASK),
                PermissionRule(pattern="bash", behavior=Behavior.DENY),
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "permissions.json"
            original.save(path)
            assert path.exists()

            reloaded = PermissionPolicy.load(path)
            assert reloaded.check("bash") == Behavior.DENY
            assert reloaded.check("write_file") == Behavior.ASK
            assert reloaded.check("read_file") == Behavior.ALLOW


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


class TestConvenienceConstructors:
    def test_make_default_policy(self):
        policy = make_default_policy()
        # doom_loop must be denied
        assert policy.check(DOOM_LOOP_TOKEN) == Behavior.DENY
        # Normal tools should be allowed
        assert policy.check("read_file") == Behavior.ALLOW

    def test_make_readonly_policy(self):
        policy = make_readonly_policy()
        # Read tools should be allowed
        assert policy.is_allowed("read_file") is True
        # Write tools must be denied
        for tool in ("write_file", "edit_file", "delete_file", "bash", "git_commit"):
            assert policy.is_denied(tool) is True, (
                f"{tool} should be denied in readonly policy"
            )
        # doom_loop must also be denied
        assert policy.is_denied(DOOM_LOOP_TOKEN) is True


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def setup_method(self):
        # Reset the singleton before each test to avoid cross-test contamination.
        reset_permission_policy(None)

    def teardown_method(self):
        reset_permission_policy(None)

    def test_get_permission_policy_returns_singleton(self):
        p1 = get_permission_policy()
        p2 = get_permission_policy()
        assert p1 is p2

    def test_reset_permission_policy(self):
        p1 = get_permission_policy()
        custom = PermissionPolicy(
            [PermissionRule(pattern="bash", behavior=Behavior.DENY)]
        )
        reset_permission_policy(custom)
        p2 = get_permission_policy()
        assert p2 is custom
        assert p2.is_denied("bash") is True

    def test_singleton_has_doom_loop_protection(self):
        """The default singleton must deny doom_loop even without a config file."""
        policy = get_permission_policy()
        assert policy.check(DOOM_LOOP_TOKEN) == Behavior.DENY
