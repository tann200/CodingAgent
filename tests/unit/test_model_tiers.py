"""tests/unit/test_model_tiers.py — Tests for S1-A model capability tiers."""

from __future__ import annotations

import pytest
from src.core.inference.model_tiers import (
    ModelTier,
    classify_model,
    get_tool_limit,
    is_simple_mode,
    supports_native_tools,
)


class TestClassifyModel:
    def test_frontier_gpt4o(self):
        assert classify_model("gpt-4o") == ModelTier.FRONTIER

    def test_frontier_gpt4o_mini(self):
        # gpt-4o-mini is a cloud model — must be FRONTIER not NANO
        assert classify_model("gpt-4o-mini") == ModelTier.FRONTIER

    def test_frontier_o1(self):
        assert classify_model("o1-preview") == ModelTier.FRONTIER

    def test_frontier_o1_mini(self):
        assert classify_model("o1-mini") == ModelTier.FRONTIER

    def test_frontier_o3_mini(self):
        assert classify_model("o3-mini") == ModelTier.FRONTIER

    def test_frontier_claude_opus(self):
        assert classify_model("claude-opus-4-6") == ModelTier.FRONTIER

    def test_frontier_claude_sonnet(self):
        assert classify_model("claude-sonnet-4-6") == ModelTier.FRONTIER

    def test_frontier_claude_haiku(self):
        assert classify_model("claude-haiku-3-5") == ModelTier.FRONTIER

    def test_frontier_gemini_2_flash(self):
        assert classify_model("gemini-2.0-flash-001") == ModelTier.FRONTIER

    def test_frontier_gemini_2_pro(self):
        assert classify_model("gemini-2.0-pro") == ModelTier.FRONTIER

    def test_frontier_gemini_flash(self):
        assert classify_model("gemini-flash") == ModelTier.FRONTIER

    def test_nano_7b(self):
        assert classify_model("qwen3:7b") == ModelTier.NANO

    def test_nano_3b(self):
        assert classify_model("llama-3b") == ModelTier.NANO

    def test_small_14b(self):
        assert classify_model("qwen3:14b") == ModelTier.SMALL

    def test_medium_32b(self):
        assert classify_model("qwen3:32b") == ModelTier.MEDIUM

    def test_large_70b(self):
        assert (
            classify_model("llama-3.1-70b") == ModelTier.MEDIUM
        )  # exactly 70 → MEDIUM

    def test_large_above_70b(self):
        assert classify_model("llama-3.1-405b") == ModelTier.LARGE

    def test_context_window_fallback_small(self):
        assert classify_model("unknown-model", context_window=4096) == ModelTier.NANO

    def test_context_window_fallback_medium(self):
        assert classify_model("unknown-model", context_window=32768) == ModelTier.MEDIUM

    def test_context_window_fallback_large(self):
        assert classify_model("unknown-model", context_window=200000) == ModelTier.LARGE

    def test_default_unknown(self):
        # Unknown model with no context window defaults to MEDIUM
        assert classify_model("mystery-model") == ModelTier.MEDIUM

    def test_mini_keyword(self):
        assert classify_model("gpt-3.5-mini") == ModelTier.NANO

    def test_deepseek_r1_is_not_frontier(self):
        # deepseek-r1 local variant — no "r2" in name → should be MEDIUM (no param count)
        assert classify_model("deepseek-r1:7b") == ModelTier.NANO


class TestToolLimits:
    @pytest.mark.parametrize(
        "tier,expected",
        [
            (ModelTier.NANO, 8),
            (ModelTier.SMALL, 20),
            (ModelTier.MEDIUM, 35),
            (ModelTier.LARGE, 50),
            (ModelTier.FRONTIER, 60),
        ],
    )
    def test_tool_limit(self, tier, expected):
        assert get_tool_limit(tier) == expected


class TestModeFlags:
    def test_simple_mode_only_nano(self):
        assert is_simple_mode(ModelTier.NANO) is True
        for tier in (
            ModelTier.SMALL,
            ModelTier.MEDIUM,
            ModelTier.LARGE,
            ModelTier.FRONTIER,
        ):
            assert is_simple_mode(tier) is False

    def test_native_tools_medium_and_above(self):
        assert supports_native_tools(ModelTier.NANO) is False
        assert supports_native_tools(ModelTier.SMALL) is False
        assert supports_native_tools(ModelTier.MEDIUM) is True
        assert supports_native_tools(ModelTier.LARGE) is True
        assert supports_native_tools(ModelTier.FRONTIER) is True

    def test_tier_values_are_strings(self):
        for tier in ModelTier:
            assert isinstance(tier.value, str)
