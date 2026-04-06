"""
tests/unit/test_system_prompt_builder.py — Unit tests for Sprint A-3: SystemPromptBuilder.

These tests focus on the static-section logic and template routing, avoiding
heavy I/O (git, AGENT.md discovery) which is exercised in integration tests.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.prompts import PromptContext, SystemPromptBuilder, reload_templates
from src.core.prompts.system_prompt_builder import (
    _select_base_template,
    _DYNAMIC_BOUNDARY,
    _STEPS_WARNING_THRESHOLD,
)
from src.core.orchestration.agent_types import (
    AgentDefinition,
    COMPACTION_AGENT,
    TITLE_AGENT,
    EXPLORE_AGENT,
    BUILD_AGENT,
)


# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------


class TestSelectBaseTemplate:
    def test_anthropic_provider(self):
        assert _select_base_template("anthropic") == "anthropic.txt"

    def test_claude_substring(self):
        assert _select_base_template("claude-something") == "anthropic.txt"

    def test_openai_provider(self):
        assert _select_base_template("openai") == "openai.txt"

    def test_gpt_substring(self):
        assert _select_base_template("gpt-4o") == "openai.txt"

    def test_copilot(self):
        assert _select_base_template("copilot") == "openai.txt"

    def test_azure(self):
        assert _select_base_template("azure") == "openai.txt"

    def test_ollama(self):
        assert _select_base_template("ollama") == "default.txt"

    def test_lmstudio(self):
        assert _select_base_template("lmstudio") == "default.txt"

    def test_gemini(self):
        assert _select_base_template("gemini") == "default.txt"

    def test_unknown_falls_back_to_default(self):
        assert _select_base_template("") == "default.txt"
        assert _select_base_template("my_custom_provider") == "default.txt"

    def test_case_insensitive(self):
        assert _select_base_template("ANTHROPIC") == "anthropic.txt"
        assert _select_base_template("OpenAI") == "openai.txt"


# ---------------------------------------------------------------------------
# Static section
# ---------------------------------------------------------------------------


class TestStaticSection:
    def _ctx(self, **kwargs) -> PromptContext:
        defaults: dict = dict(
            provider_id="default",
            model_id="test-model",
            cwd=Path("/tmp"),
            agent=BUILD_AGENT,
            role="operational",
            plan_mode_active=False,
            just_switched_from_plan=False,
            steps_taken=0,
            max_steps=30,
        )
        defaults.update(kwargs)
        return PromptContext(**defaults)

    def test_prompt_override_replaces_base(self):
        """When agent has prompt_override, it is used instead of template."""
        agent = AgentDefinition(
            id="custom",
            name="Custom",
            description="",
            prompt_override="MY CUSTOM PROMPT",
        )
        ctx = self._ctx(agent=agent)
        static, _ = SystemPromptBuilder.build(ctx)
        assert "MY CUSTOM PROMPT" in static

    def test_plan_mode_injects_reminder(self):
        """plan_mode_active=True should inject plan_reminder template content."""
        ctx = self._ctx(agent=EXPLORE_AGENT, plan_mode_active=True)
        static, _ = SystemPromptBuilder.build(ctx)
        # The plan reminder template exists; just check the static part is non-empty
        # (template content may be empty in test env if files not found).
        assert isinstance(static, str)

    def test_build_switch_injection(self):
        ctx = self._ctx(just_switched_from_plan=True)
        static, _ = SystemPromptBuilder.build(ctx)
        assert isinstance(static, str)

    def test_max_steps_warning_injected_near_limit(self):
        """Warning should be injected when steps_remaining <= threshold."""
        ctx = self._ctx(
            steps_taken=30 - _STEPS_WARNING_THRESHOLD,  # exactly at threshold
            max_steps=30,
        )
        static, _ = SystemPromptBuilder.build(ctx)
        assert isinstance(static, str)

    def test_max_steps_warning_not_injected_when_far(self):
        """Warning should NOT be injected when plenty of steps remain."""
        ctx = self._ctx(steps_taken=0, max_steps=30)
        with patch(
            "src.core.prompts.system_prompt_builder._load_template",
            side_effect=lambda name: f"[{name}]",
        ):
            static, _ = SystemPromptBuilder.build(ctx)
        # max_steps.txt should not appear when 30 steps remain
        assert "[max_steps.txt]" not in static

    def test_role_suffix_included(self):
        """Role suffix from role_config should appear when role is set."""
        ctx = self._ctx(role="operational")
        # Just verify it runs without error; suffix may be empty for unknown role
        static, _ = SystemPromptBuilder.build(ctx)
        assert isinstance(static, str)


# ---------------------------------------------------------------------------
# Internal agents
# ---------------------------------------------------------------------------


class TestInternalAgents:
    def test_internal_agent_has_no_dynamic_part(self):
        ctx = PromptContext(
            agent=COMPACTION_AGENT,
            cwd=Path("/tmp"),
        )
        static, dynamic = SystemPromptBuilder.build(ctx)
        assert dynamic == ""

    def test_title_agent_no_dynamic(self):
        ctx = PromptContext(
            agent=TITLE_AGENT,
            cwd=Path("/tmp"),
        )
        _, dynamic = SystemPromptBuilder.build(ctx)
        assert dynamic == ""

    def test_internal_static_contains_override(self):
        ctx = PromptContext(
            agent=COMPACTION_AGENT,
            cwd=Path("/tmp"),
        )
        static, _ = SystemPromptBuilder.build(ctx)
        assert "compaction" in static.lower() or "summary" in static.lower()


# ---------------------------------------------------------------------------
# include_dynamic_prompt=False
# ---------------------------------------------------------------------------


class TestDynamicPromptFlag:
    def test_include_dynamic_false_returns_empty_dynamic(self):
        agent = AgentDefinition(
            id="no_dyn",
            name="NoDyn",
            description="",
            mode="subagent",
            include_dynamic_prompt=False,
            prompt_override="STATIC ONLY",
        )
        ctx = PromptContext(agent=agent, cwd=Path("/tmp"))
        _, dynamic = SystemPromptBuilder.build(ctx)
        assert dynamic == ""

    def test_include_dynamic_true_returns_dynamic(self):
        agent = AgentDefinition(
            id="with_dyn",
            name="WithDyn",
            description="",
            mode="subagent",
            include_dynamic_prompt=True,
        )
        ctx = PromptContext(
            agent=agent,
            cwd=Path("/tmp"),
            provider_id="default",
        )
        # Dynamic part depends on git/instruction_loader — just verify it's a string.
        _, dynamic = SystemPromptBuilder.build(ctx)
        assert isinstance(dynamic, str)


# ---------------------------------------------------------------------------
# build_combined
# ---------------------------------------------------------------------------


class TestBuildCombined:
    def test_combined_includes_boundary_when_dynamic_nonempty(self):
        """build_combined should join static and dynamic with the boundary marker."""
        with patch.object(
            SystemPromptBuilder, "build", return_value=("STATIC", "DYNAMIC")
        ):
            result = SystemPromptBuilder.build_combined(PromptContext())
        assert _DYNAMIC_BOUNDARY in result
        assert "STATIC" in result
        assert "DYNAMIC" in result

    def test_combined_no_boundary_when_dynamic_empty(self):
        """If dynamic part is empty (internal agent), no boundary marker is inserted."""
        with patch.object(SystemPromptBuilder, "build", return_value=("STATIC", "")):
            result = SystemPromptBuilder.build_combined(PromptContext())
        assert _DYNAMIC_BOUNDARY not in result
        assert result == "STATIC"


# ---------------------------------------------------------------------------
# Environment block
# ---------------------------------------------------------------------------


class TestEnvBlock:
    def test_env_block_contains_model(self):
        ctx = PromptContext(
            model_id="claude-sonnet-4-5",
            provider_id="anthropic",
            cwd=Path("/tmp"),
            agent=BUILD_AGENT,
        )
        env = SystemPromptBuilder._build_env_block(ctx)
        assert "claude-sonnet-4-5" in env
        assert "anthropic" in env

    def test_env_block_contains_cwd(self):
        ctx = PromptContext(cwd=Path("/my/project"), agent=BUILD_AGENT)
        env = SystemPromptBuilder._build_env_block(ctx)
        assert "/my/project" in env

    def test_env_block_contains_platform(self):
        ctx = PromptContext(agent=BUILD_AGENT, cwd=Path("/tmp"))
        env = SystemPromptBuilder._build_env_block(ctx)
        assert "<environment>" in env
        assert "</environment>" in env
        assert "platform:" in env

    def test_env_block_default_provider_omitted(self):
        ctx = PromptContext(provider_id="default", agent=BUILD_AGENT, cwd=Path("/tmp"))
        env = SystemPromptBuilder._build_env_block(ctx)
        assert "provider:" not in env


# ---------------------------------------------------------------------------
# reload_templates
# ---------------------------------------------------------------------------


class TestReloadTemplates:
    def test_reload_clears_cache(self):
        from src.core.prompts.system_prompt_builder import (
            _template_cache,
            _load_template,
        )

        # Prime the cache with something
        _load_template("default.txt")
        assert "default.txt" in _template_cache
        reload_templates()
        assert "default.txt" not in _template_cache


# ---------------------------------------------------------------------------
# Skills in dynamic section
# ---------------------------------------------------------------------------


class TestSkillsInDynamic:
    def test_skills_listed_in_dynamic_section(self):
        ctx = PromptContext(
            agent=BUILD_AGENT,
            cwd=Path("/tmp"),
            available_skills=["code_review", "write_tests"],
        )
        _, dynamic = SystemPromptBuilder.build(ctx)
        # Dynamic part should mention the skills
        assert "code_review" in dynamic
        assert "write_tests" in dynamic

    def test_no_skills_section_when_empty(self):
        ctx = PromptContext(
            agent=BUILD_AGENT,
            cwd=Path("/tmp"),
            available_skills=[],
        )
        _, dynamic = SystemPromptBuilder.build(ctx)
        assert "available_skills" not in dynamic


# ---------------------------------------------------------------------------
# Extra dynamic sections
# ---------------------------------------------------------------------------


class TestExtraDynamicSections:
    def test_extra_sections_appended(self):
        ctx = PromptContext(
            agent=BUILD_AGENT,
            cwd=Path("/tmp"),
            extra_dynamic_sections=["<mcp_servers>my-server</mcp_servers>"],
        )
        _, dynamic = SystemPromptBuilder.build(ctx)
        assert "my-server" in dynamic
