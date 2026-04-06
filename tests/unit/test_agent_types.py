"""
tests/unit/test_agent_types.py — Unit tests for Sprint A-1/A-2: AgentDefinition + AgentRegistry.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.core.orchestration.agent_types import (
    AgentDefinition,
    AgentRegistry,
    BUILD_AGENT,
    COMPACTION_AGENT,
    EXPLORE_AGENT,
    PLAN_AGENT,
    TITLE_AGENT,
    VERIFICATION_AGENT,
    get_agent,
    get_agent_or_default,
    get_agent_registry,
)


# ---------------------------------------------------------------------------
# AgentDefinition
# ---------------------------------------------------------------------------


class TestAgentDefinition:
    def test_basic_fields(self):
        agent = AgentDefinition(
            id="test",
            name="Test Agent",
            description="A test agent.",
        )
        assert agent.id == "test"
        assert agent.name == "Test Agent"
        assert agent.mode == "primary"  # default
        assert agent.include_dynamic_prompt is True
        assert agent.allowed_tools is None
        assert agent.denied_tools == set()
        assert agent.max_rounds == 20

    def test_is_tool_permitted_no_restrictions(self):
        agent = AgentDefinition(id="a", name="A", description="")
        assert agent.is_tool_permitted("read_file") is True
        assert agent.is_tool_permitted("write_file") is True

    def test_is_tool_permitted_allowlist(self):
        agent = AgentDefinition(
            id="a", name="A", description="", allowed_tools={"read_file", "grep"}
        )
        assert agent.is_tool_permitted("read_file") is True
        assert agent.is_tool_permitted("grep") is True
        assert agent.is_tool_permitted("write_file") is False  # not in allowlist

    def test_is_tool_permitted_denylist(self):
        agent = AgentDefinition(
            id="a", name="A", description="", denied_tools={"bash", "write_file"}
        )
        assert agent.is_tool_permitted("bash") is False
        assert agent.is_tool_permitted("write_file") is False
        assert agent.is_tool_permitted("read_file") is True

    def test_is_tool_permitted_deny_beats_allow(self):
        agent = AgentDefinition(
            id="a",
            name="A",
            description="",
            allowed_tools={"read_file", "bash"},
            denied_tools={"bash"},
        )
        # bash is in both allowed AND denied — deny wins
        assert agent.is_tool_permitted("bash") is False
        assert agent.is_tool_permitted("read_file") is True

    def test_roundtrip_to_from_dict(self):
        original = AgentDefinition(
            id="custom",
            name="Custom",
            description="A custom agent.",
            mode="subagent",
            allowed_tools={"read_file", "grep"},
            denied_tools={"bash"},
            temperature=0.3,
            max_rounds=5,
            hidden=True,
            extra={"author": "test"},
        )
        d = original.to_dict()
        restored = AgentDefinition.from_dict(d)

        assert restored.id == original.id
        assert restored.name == original.name
        assert restored.mode == original.mode
        assert restored.allowed_tools == original.allowed_tools
        assert restored.denied_tools == original.denied_tools
        assert restored.temperature == original.temperature
        assert restored.max_rounds == original.max_rounds
        assert restored.hidden == original.hidden
        assert restored.extra == original.extra


# ---------------------------------------------------------------------------
# Built-in agents
# ---------------------------------------------------------------------------


class TestBuiltinAgents:
    def test_build_agent_basics(self):
        assert BUILD_AGENT.id == "build"
        assert BUILD_AGENT.mode == "primary"
        assert BUILD_AGENT.toolset == "coding"
        assert BUILD_AGENT.hidden is False

    def test_explore_agent_is_readonly(self):
        assert EXPLORE_AGENT.id == "explore"
        assert EXPLORE_AGENT.mode == "subagent"
        # Must have an allowlist (not None)
        assert EXPLORE_AGENT.allowed_tools is not None
        # Must deny write tools
        assert "write_file" in EXPLORE_AGENT.denied_tools
        assert "bash" in EXPLORE_AGENT.denied_tools
        # Must not permit write tools (allowlist check)
        assert EXPLORE_AGENT.is_tool_permitted("write_file") is False

    def test_plan_agent_denies_writes(self):
        assert PLAN_AGENT.id == "plan"
        for tool in ("write_file", "edit_file", "bash", "git_commit"):
            assert PLAN_AGENT.is_tool_permitted(tool) is False, (
                f"{tool} should be denied"
            )

    def test_verification_agent_can_run_tests(self):
        assert VERIFICATION_AGENT.id == "verification"
        assert VERIFICATION_AGENT.is_tool_permitted("run_tests") is True
        assert VERIFICATION_AGENT.is_tool_permitted("bash") is True
        assert VERIFICATION_AGENT.is_tool_permitted("write_file") is False

    def test_compaction_agent_is_internal(self):
        assert COMPACTION_AGENT.mode == "internal"
        assert COMPACTION_AGENT.hidden is True
        assert COMPACTION_AGENT.include_dynamic_prompt is False
        assert COMPACTION_AGENT.max_rounds == 1

    def test_title_agent_is_internal(self):
        assert TITLE_AGENT.mode == "internal"
        assert TITLE_AGENT.hidden is True
        assert TITLE_AGENT.max_rounds == 1
        assert TITLE_AGENT.temperature == 0.5


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------


class TestAgentRegistry:
    def _fresh_registry(self) -> AgentRegistry:
        """Return a registry without loading custom agents from disk."""
        return AgentRegistry()

    def test_builtin_agents_present(self):
        reg = self._fresh_registry()
        for agent_id in (
            "build",
            "explore",
            "plan",
            "verification",
            "compaction",
            "title",
        ):
            assert reg.get(agent_id) is not None, f"Missing builtin agent: {agent_id}"

    def test_get_missing_returns_none(self):
        reg = self._fresh_registry()
        assert reg.get("nonexistent") is None

    def test_get_or_default_falls_back(self):
        reg = self._fresh_registry()
        result = reg.get_or_default("nonexistent")
        assert result.id == "build"

    def test_get_or_default_none_falls_back(self):
        reg = self._fresh_registry()
        result = reg.get_or_default(None)
        assert result.id == "build"

    def test_register_custom_agent(self):
        reg = self._fresh_registry()
        custom = AgentDefinition(id="custom_a", name="Custom A", description="")
        reg.register(custom)
        assert reg.get("custom_a") is custom

    def test_register_conflicts_with_builtin(self):
        reg = self._fresh_registry()
        conflicting = AgentDefinition(id="build", name="Override Build", description="")
        with pytest.raises(ValueError, match="conflicts with a built-in"):
            reg.register(conflicting)

    def test_register_builtin_override_allowed(self):
        reg = self._fresh_registry()
        new_build = AgentDefinition(id="build", name="Custom Build", description="")
        reg.register(new_build, allow_override=True)
        found = reg.get("build")
        assert found is not None
        assert found.name == "Custom Build"

    def test_list_excludes_hidden_by_default(self):
        reg = self._fresh_registry()
        visible = reg.list()
        hidden_ids = {a.id for a in visible if a.hidden}
        assert not hidden_ids, f"Hidden agents in visible list: {hidden_ids}"

    def test_list_include_hidden(self):
        reg = self._fresh_registry()
        all_agents = reg.list(include_hidden=True)
        ids = {a.id for a in all_agents}
        assert "compaction" in ids
        assert "title" in ids

    def test_list_filter_by_mode(self):
        reg = self._fresh_registry()
        subagents = reg.list(include_hidden=True, mode="subagent")
        for a in subagents:
            assert a.mode == "subagent"
        assert any(a.id == "explore" for a in subagents)

    def test_load_custom_agents_from_json(self):
        reg = self._fresh_registry()
        custom_data = [
            {
                "id": "json_custom",
                "name": "JSON Custom",
                "description": "Loaded from JSON.",
                "mode": "subagent",
            }
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(custom_data, f)
            path = Path(f.name)
        try:
            count = reg.load_custom_agents(path)
            assert count == 1
            loaded = reg.get("json_custom")
            assert loaded is not None
            assert loaded.name == "JSON Custom"
        finally:
            path.unlink(missing_ok=True)

    def test_load_custom_agents_missing_file(self):
        reg = self._fresh_registry()
        count = reg.load_custom_agents(Path("/nonexistent/agents.json"))
        assert count == 0

    def test_save_and_reload_custom_agents(self):
        reg = self._fresh_registry()
        custom = AgentDefinition(
            id="saved_agent", name="Saved", description="Persisted.", mode="subagent"
        )
        reg.register(custom)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "agents.json"
            reg.save_custom_agents(path)
            assert path.exists()

            reg2 = self._fresh_registry()
            reg2.load_custom_agents(path)
            loaded = reg2.get("saved_agent")
            assert loaded is not None
            assert loaded.name == "Saved"


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


class TestModuleLevelFunctions:
    def test_get_agent_returns_builtin(self):
        # Uses the global singleton — just check it finds builtins.
        result = get_agent("build")
        assert result is not None
        assert result.id == "build"

    def test_get_agent_returns_none_for_unknown(self):
        result = get_agent("does_not_exist_xyzzy")
        assert result is None

    def test_get_agent_or_default_returns_build_fallback(self):
        result = get_agent_or_default("does_not_exist_xyzzy")
        assert result.id == "build"

    def test_get_agent_registry_returns_singleton(self):
        reg1 = get_agent_registry()
        reg2 = get_agent_registry()
        assert reg1 is reg2
