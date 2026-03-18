"""
Tests for AgentBrainManager.
"""

import pytest
from unittest.mock import patch, MagicMock
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
        roles_before = manager.get_all_roles()
        manager.reload()
        roles_after = manager.get_all_roles()
        assert isinstance(roles_after, dict)
