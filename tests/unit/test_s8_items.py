"""tests/unit/test_s8_items.py — Tests for S8-A (schema stripping) and S8-B (simple_mode).

S8-A: ContextBuilder renders short tool descriptions for NANO/SMALL tiers.
S8-B: NANO tier forces YAML-only output format (simple_mode), no native tools.
"""


# ruff: noqa: E501
from __future__ import annotations

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tools(*names_descs):
    """Return a list of minimal tool dicts for testing."""
    return [
        {"name": n, "description": d}
        for n, d in names_descs
    ]


# ---------------------------------------------------------------------------
# S8-A — _render_tools_for_tier
# ---------------------------------------------------------------------------

class TestRenderToolsForTier:
    def _get_builder(self):
        from src.core.context.context_builder import ContextBuilder
        return ContextBuilder()

    def test_medium_tier_keeps_full_description(self):
        builder = self._get_builder()
        tools = _make_tools(
            ("read_file", "Reads a file. Returns its content. Accepts a path argument."),
        )
        rendered = builder._render_tools_for_tier(tools, "medium")
        assert "Reads a file. Returns its content." in rendered

    def test_nano_tier_truncates_to_first_sentence(self):
        builder = self._get_builder()
        tools = _make_tools(
            ("read_file", "Reads a file. Returns its content. Accepts a path argument."),
        )
        rendered = builder._render_tools_for_tier(tools, "nano")
        # Only first sentence should appear
        assert "Reads a file." in rendered
        # Second sentence should NOT appear
        assert "Returns its content." not in rendered

    def test_small_tier_truncates_like_nano(self):
        builder = self._get_builder()
        tools = _make_tools(
            ("bash", "Executes a shell command. Use with caution. Returns stdout."),
        )
        rendered = builder._render_tools_for_tier(tools, "small")
        assert "Executes a shell command." in rendered
        assert "Use with caution." not in rendered

    def test_frontier_keeps_full_description(self):
        builder = self._get_builder()
        tools = _make_tools(
            ("search", "Searches codebase. Returns matching lines. Supports regex."),
        )
        rendered = builder._render_tools_for_tier(tools, "frontier")
        assert "Searches codebase. Returns matching lines." in rendered

    def test_none_tier_keeps_full_description(self):
        """None tier defaults to MEDIUM — no truncation."""
        builder = self._get_builder()
        tools = _make_tools(
            ("glob", "Finds files. Supports wildcards. Returns paths."),
        )
        rendered = builder._render_tools_for_tier(tools, None)
        assert "Supports wildcards." in rendered

    def test_multiple_tools_all_rendered(self):
        builder = self._get_builder()
        tools = _make_tools(
            ("read_file", "Reads file."),
            ("write_file", "Writes file."),
        )
        rendered = builder._render_tools_for_tier(tools, "nano")
        assert "read_file" in rendered
        assert "write_file" in rendered

    def test_empty_description_handled_gracefully(self):
        builder = self._get_builder()
        tools = [{"name": "mystery_tool", "description": ""}]
        rendered = builder._render_tools_for_tier(tools, "nano")
        assert "mystery_tool" in rendered

    def test_description_without_period_kept_intact_for_nano(self):
        """No period in description → first 'sentence' is the whole description."""
        builder = self._get_builder()
        tools = _make_tools(("tool_x", "Does something useful with no period"))
        rendered = builder._render_tools_for_tier(tools, "nano")
        assert "Does something useful with no period" in rendered


# ---------------------------------------------------------------------------
# S8-B — simple_mode output format in build_prompt
# ---------------------------------------------------------------------------

class TestSimpleModeBuildPrompt:
    """Verify that NANO tier produces simple_mode output format instructions."""

    def _minimal_build(self, model_tier):
        from src.core.context.context_builder import ContextBuilder
        builder = ContextBuilder()
        messages = builder.build_prompt(
            role_name="operational",
            active_skills=[],
            task_description="Do something",
            tools=[{"name": "read_file", "description": "Reads a file."}],
            conversation=[],
            model_tier=model_tier,
        )
        # Flatten all message content
        return "\n".join(m.get("content", "") for m in messages)

    def test_nano_tier_has_strict_one_tool_rule(self):
        content = self._minimal_build("nano")
        assert "ONE tool call" in content or "EXACTLY ONE" in content

    def test_nano_tier_no_native_tools_instruction(self):
        """NANO must not instruct use of native JSON function calling."""
        content = self._minimal_build("nano")
        assert "native JSON function calling API" not in content

    def test_nano_tier_yaml_format_present(self):
        content = self._minimal_build("nano")
        assert "yaml" in content.lower()

    def test_medium_tier_has_standard_format(self):
        content = self._minimal_build("medium")
        # Standard format includes think tags and tool format instructions
        assert "YAML tool format" in content or "yaml" in content.lower()
        # Should NOT have the strict one-tool rule
        assert "EXACTLY ONE tool call" not in content

    def test_frontier_tier_behaves_like_medium_without_native_caps(self):
        """Without provider_capabilities saying native tools, frontier uses YAML format."""
        content = self._minimal_build("frontier")
        assert "yaml" in content.lower()
        assert "EXACTLY ONE tool call" not in content


# ---------------------------------------------------------------------------
# S8-B — is_simple_mode integration
# ---------------------------------------------------------------------------

class TestIsSimpleMode:
    def test_nano_is_simple(self):
        from src.core.inference.model_tiers import ModelTier, is_simple_mode
        assert is_simple_mode(ModelTier.NANO) is True

    def test_small_not_simple(self):
        from src.core.inference.model_tiers import ModelTier, is_simple_mode
        assert is_simple_mode(ModelTier.SMALL) is False

    def test_medium_not_simple(self):
        from src.core.inference.model_tiers import ModelTier, is_simple_mode
        assert is_simple_mode(ModelTier.MEDIUM) is False

    def test_frontier_not_simple(self):
        from src.core.inference.model_tiers import ModelTier, is_simple_mode
        assert is_simple_mode(ModelTier.FRONTIER) is False


# ---------------------------------------------------------------------------
# S8-A + S8-B Integration — tool description token budget
# ---------------------------------------------------------------------------

class TestSchemaStrippingTokenSavings:
    """Verify NANO descriptions are meaningfully shorter than MEDIUM descriptions."""

    def test_nano_descriptions_shorter_than_medium(self):
        from src.core.context.context_builder import ContextBuilder
        builder = ContextBuilder()
        long_desc = (
            "Reads the full text content of a file from the filesystem. "
            "Accepts a path argument. Returns the raw text as a string. "
            "Binary files may cause encoding errors."
        )
        tools = [{"name": "read_file", "description": long_desc}]
        nano_render = builder._render_tools_for_tier(tools, "nano")
        medium_render = builder._render_tools_for_tier(tools, "medium")
        assert len(nano_render) < len(medium_render)
