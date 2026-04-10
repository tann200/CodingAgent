"""
Tests for AgentBrainManager.
"""

from src.core.orchestration.agent_brain import (
    AgentBrainManager,
    get_agent_brain_manager,
    _parse_front_matter,
    _extract_body,
)


class TestParseFrontMatter:
    """Tests for front-matter parsing."""

    def test_parse_front_matter_with_valid_yaml(self):
        """Test parsing valid YAML front-matter."""
        text = """---
name: test
version: "1.0"
---
Body content here."""
        result = _parse_front_matter(text)
        assert result is not None
        assert result.get("name") == "test"
        assert result.get("version") == "1.0"

    def test_parse_front_matter_without_front_matter(self):
        """Test parsing text without front-matter."""
        text = "Just plain text without front-matter."
        result = _parse_front_matter(text)
        assert result is None

    def test_parse_front_matter_empty(self):
        """Test parsing empty text."""
        result = _parse_front_matter("")
        assert result is None


class TestExtractBody:
    """Tests for body extraction."""

    def test_extract_body_with_front_matter(self):
        """Test extracting body after front-matter."""
        text = """---
key: value
---
This is the body."""
        result = _extract_body(text)
        assert "This is the body" in result

    def test_extract_body_without_front_matter(self):
        """Test extracting body without front-matter."""
        text = "Plain body text"
        result = _extract_body(text)
        assert result == "Plain body text"


class TestAgentBrainManager:
    """Tests for AgentBrainManager."""

    def test_singleton_behavior(self):
        """Test that AgentBrainManager is a singleton."""
        manager1 = AgentBrainManager()
        manager2 = AgentBrainManager()
        assert manager1 is manager2

    def test_get_agent_brain_manager_function(self):
        """Test get_agent_brain_manager returns manager."""
        manager = get_agent_brain_manager()
        assert manager is not None
        assert isinstance(manager, AgentBrainManager)

    def test_get_identity_default(self):
        """Test getting default identity."""
        manager = AgentBrainManager()
        identity = manager.get_identity("soul")
        assert isinstance(identity, str)

    def test_get_identity_nonexistent(self):
        """Test getting nonexistent identity returns empty."""
        manager = AgentBrainManager()
        result = manager.get_identity("nonexistent")
        assert result == ""

    def test_get_role(self):
        """Test getting a role."""
        manager = AgentBrainManager()
        role = manager.get_role("operational")
        assert isinstance(role, str)

    def test_get_role_nonexistent(self):
        """Test getting nonexistent role returns empty."""
        manager = AgentBrainManager()
        result = manager.get_role("nonexistent_role_xyz")
        assert result == ""

    def test_get_skill(self):
        """Test getting a skill."""
        manager = AgentBrainManager()
        skill = manager.get_skill("context_hygiene")
        assert isinstance(skill, str)

    def test_get_all_roles(self):
        """Test getting all roles returns dict."""
        manager = AgentBrainManager()
        roles = manager.get_all_roles()
        assert isinstance(roles, dict)

    def test_get_all_skills(self):
        """Test getting all skills returns dict."""
        manager = AgentBrainManager()
        skills = manager.get_all_skills()
        assert isinstance(skills, dict)

    def test_compile_system_prompt_default(self):
        """Test compiling system prompt with default role."""
        manager = AgentBrainManager()
        prompt = manager.compile_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_compile_system_prompt_with_role(self):
        """Test compiling system prompt with specific role."""
        manager = AgentBrainManager()
        prompt = manager.compile_system_prompt("operational")
        assert isinstance(prompt, str)

    def test_reload_clears_and_reloads(self):
        """Test reload clears caches and reloads."""
        manager = AgentBrainManager()
        _ = manager.get_all_roles()
        manager.reload()
        roles_after = manager.get_all_roles()
        assert isinstance(roles_after, dict)


class TestSkillsAutoListing:
    """SK-1 tests: skills are auto-listed in the system prompt."""

    def test_list_skills_summary_returns_string(self):
        """list_skills_summary() returns a non-empty string when skills exist."""
        manager = AgentBrainManager()
        summary = manager.list_skills_summary()
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_list_skills_summary_contains_bullet_entries(self):
        """Each skill appears as a bullet line in the summary."""
        manager = AgentBrainManager()
        summary = manager.list_skills_summary()
        # Should have at least one bullet entry
        assert "•" in summary

    def test_list_skills_summary_includes_known_skill(self):
        """Skills with frontmatter appear by their name field."""
        manager = AgentBrainManager()
        summary = manager.list_skills_summary()
        # code_review has frontmatter name: code_review
        assert "code_review" in summary

    def test_compile_system_prompt_includes_available_skills_section(self):
        """compile_system_prompt() injects an <available_skills> block."""
        manager = AgentBrainManager()
        prompt = manager.compile_system_prompt("operational")
        assert "<available_skills>" in prompt
        assert "</available_skills>" in prompt

    def test_compile_system_prompt_skills_section_has_load_skill_hint(self):
        """The injected skills block mentions how to use a skill."""
        manager = AgentBrainManager()
        prompt = manager.compile_system_prompt()
        assert "load_skill" in prompt

    def test_legacy_compile_system_prompt_also_injects_skills(self):
        """_compile_system_prompt (legacy path) also injects the skills block."""
        from src.core.orchestration.agent_brain import _compile_system_prompt

        result = _compile_system_prompt("You are a test assistant.")
        assert "<available_skills>" in result

    def test_reload_preserves_skill_meta_cache(self):
        """After reload, list_skills_summary() still works."""
        manager = AgentBrainManager()
        manager.reload()
        summary = manager.list_skills_summary()
        assert isinstance(summary, str)
        assert len(summary) > 0
