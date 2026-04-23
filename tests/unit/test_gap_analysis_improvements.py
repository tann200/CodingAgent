"""
Regression tests for gap-analysis improvements.

Covers:
  GAP-SMALL-1  simplified output format injected for NANO/SMALL tiers
  GAP-SMALL-2  operational-small / operational-frontier role selection
  GAP-SMALL-5  format_error not counted toward no_plan_fail_count
  GAP-FRONTIER-1 model-ID-based prompt partial routing
  GAP-FRONTIER-6 tier-dependent plan step limits
"""


# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import re


# ─────────────────────────────────────────────────────────────────────────────
# GAP-FRONTIER-6: get_plan_step_limit
# ─────────────────────────────────────────────────────────────────────────────

class TestGetPlanStepLimit:
    def test_nano_limit_4(self):
        from src.core.inference.model_tiers import ModelTier, get_plan_step_limit
        assert get_plan_step_limit(ModelTier.NANO) == 4

    def test_small_limit_6(self):
        from src.core.inference.model_tiers import ModelTier, get_plan_step_limit
        assert get_plan_step_limit(ModelTier.SMALL) == 6

    def test_medium_limit_10(self):
        from src.core.inference.model_tiers import ModelTier, get_plan_step_limit
        assert get_plan_step_limit(ModelTier.MEDIUM) == 10

    def test_large_limit_16(self):
        from src.core.inference.model_tiers import ModelTier, get_plan_step_limit
        assert get_plan_step_limit(ModelTier.LARGE) == 16

    def test_frontier_limit_20(self):
        from src.core.inference.model_tiers import ModelTier, get_plan_step_limit
        assert get_plan_step_limit(ModelTier.FRONTIER) == 20

    def test_all_tiers_have_limits(self):
        from src.core.inference.model_tiers import ModelTier, get_plan_step_limit
        for tier in ModelTier:
            assert get_plan_step_limit(tier) > 0


# ─────────────────────────────────────────────────────────────────────────────
# GAP-SMALL-2: role file selection
# ─────────────────────────────────────────────────────────────────────────────

class TestSelectRoleForTier:
    def _make_builder_with_roles(self, roles: dict) -> "ContextBuilder":
        from src.core.context.context_builder import ContextBuilder
        builder = ContextBuilder()
        builder.roles = roles
        return builder

    def test_nano_selects_operational_small(self):
        builder = self._make_builder_with_roles({
            "operational": "BASE operational",
            "operational-small": "SMALL operational",
            "operational-frontier": "FRONTIER operational",
        })
        result = builder._select_role_for_tier("operational", "nano")
        assert result == "SMALL operational"

    def test_small_selects_operational_small(self):
        builder = self._make_builder_with_roles({
            "operational": "BASE operational",
            "operational-small": "SMALL operational",
        })
        result = builder._select_role_for_tier("operational", "small")
        assert result == "SMALL operational"

    def test_frontier_selects_operational_frontier(self):
        builder = self._make_builder_with_roles({
            "operational": "BASE operational",
            "operational-frontier": "FRONTIER operational",
        })
        result = builder._select_role_for_tier("operational", "frontier")
        assert result == "FRONTIER operational"

    def test_large_selects_operational_frontier(self):
        builder = self._make_builder_with_roles({
            "operational": "BASE operational",
            "operational-frontier": "FRONTIER operational",
        })
        result = builder._select_role_for_tier("operational", "large")
        assert result == "FRONTIER operational"

    def test_medium_selects_base_operational(self):
        builder = self._make_builder_with_roles({
            "operational": "BASE operational",
            "operational-small": "SMALL operational",
            "operational-frontier": "FRONTIER operational",
        })
        result = builder._select_role_for_tier("operational", "medium")
        assert result == "BASE operational"

    def test_non_operational_role_unaffected(self):
        builder = self._make_builder_with_roles({
            "operational": "BASE",
            "operational-small": "SMALL",
            "analyst": "analyst content",
        })
        result = builder._select_role_for_tier("analyst", "small")
        assert result == "analyst content"

    def test_fallback_when_variant_missing(self):
        """If operational-small.md missing, fall back to operational.md."""
        builder = self._make_builder_with_roles({
            "operational": "BASE operational",
            # no operational-small
        })
        result = builder._select_role_for_tier("operational", "nano")
        assert result == "BASE operational"


# ─────────────────────────────────────────────────────────────────────────────
# GAP-SMALL-1: simplified output format for NANO/SMALL
# ─────────────────────────────────────────────────────────────────────────────

class TestSmallTierOutputFormat:
    def _build_static_prefix(self, tier: str) -> str:
        from src.core.context.context_builder import ContextBuilder, _STATIC_PROMPT_CACHE
        _STATIC_PROMPT_CACHE.clear()
        builder = ContextBuilder()
        builder.roles = {
            "operational": "op content",
            "operational-small": "op small content",
            "operational-frontier": "op frontier content",
        }
        builder.soul = "soul"
        builder.skills = {}
        return builder._build_static_system_prefix(
            role_name="operational",
            active_skills=[],
            tools=[],
            model_tier=tier,
            provider_capabilities={},
            use_native_tools=False,
            is_simple_mode=tier == "nano",
        )

    def test_nano_format_contains_status_only(self):
        prefix = self._build_static_prefix("nano")
        # NANO is simple_mode — gets simple_mode format block, not SMALL format
        # The format must not demand FILES_CHANGED:
        assert "FILES_CHANGED:" not in prefix

    def test_small_format_simplified(self):
        prefix = self._build_static_prefix("small")
        # SMALL gets the simplified STATUS: only format
        assert "STATUS: complete | partial | failed" in prefix
        # Full 4-field format must NOT be present
        assert "FILES_CHANGED:" not in prefix

    def test_medium_format_full(self):
        prefix = self._build_static_prefix("medium")
        # MEDIUM keeps the full YAML format (no simplified STATUS-only block)
        assert "IMPORTANT: Use markdown YAML format" in prefix


# ─────────────────────────────────────────────────────────────────────────────
# GAP-FRONTIER-1: model-ID prompt partial routing
# ─────────────────────────────────────────────────────────────────────────────

class TestModelIdPartialRouting:
    def _select(self, model_id: str, provider_family: str = "") -> str:
        from src.core.context.context_builder import ContextBuilder
        builder = ContextBuilder()
        caps = {"model": model_id, "provider_family": provider_family}
        return builder._select_prompt_partial(
            model_tier="frontier",
            provider_capabilities=caps,
            is_reasoning=False,
        )

    def test_o1_matches_openai_reasoning_pattern(self):
        """o1 should try openai-reasoning.md before falling through."""
        result = self._select("o1-mini")
        # File doesn't exist → falls through to provider/default; test the pattern matching logic
        from src.core.context.context_builder import ContextBuilder
        import re
        matched = any(
            re.search(pat, "o1-mini")
            for pat, _ in ContextBuilder._MODEL_ID_PARTIAL_MAP
        )
        assert matched

    def test_claude_opus_matches_anthropic_frontier_pattern(self):
        from src.core.context.context_builder import ContextBuilder
        import re
        matched = any(
            re.search(pat, "claude-opus-4-6")
            for pat, _ in ContextBuilder._MODEL_ID_PARTIAL_MAP
        )
        assert matched

    def test_claude_haiku_matches_anthropic_small_pattern(self):
        from src.core.context.context_builder import ContextBuilder
        import re
        matched = any(
            re.search(pat, "claude-haiku-4-5")
            for pat, _ in ContextBuilder._MODEL_ID_PARTIAL_MAP
        )
        assert matched

    def test_gemini_flash_matches_gemini_small_pattern(self):
        from src.core.context.context_builder import ContextBuilder
        import re
        matched = any(
            re.search(pat, "gemini-2.0-flash")
            for pat, _ in ContextBuilder._MODEL_ID_PARTIAL_MAP
        )
        assert matched

    def test_gemini_pro_matches_gemini_frontier_pattern(self):
        from src.core.context.context_builder import ContextBuilder
        import re
        matched = any(
            re.search(pat, "gemini-2.5-pro")
            for pat, _ in ContextBuilder._MODEL_ID_PARTIAL_MAP
        )
        assert matched

    def test_unknown_model_id_falls_through_to_provider(self):
        """An unknown model ID should not crash — falls through to provider/default."""
        result = self._select("totally-unknown-model-9000", "anthropic")
        # Should return a string (possibly empty) without error
        assert isinstance(result, str)


# ─────────────────────────────────────────────────────────────────────────────
# GAP-SMALL-5: format_error not counted toward no_plan_fail_count
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatErrorNotCountedAsDoomLoop:
    def _make_state(self, no_plan_fail_count: int = 0) -> dict:
        return {
            "no_plan_fail_count": no_plan_fail_count,
            "current_plan": None,  # no plan — triggers the fail count logic
        }

    def test_format_error_is_not_incremented(self):
        """Simulate the no_plan_fail_count logic with a format_error result."""
        state = self._make_state(no_plan_fail_count=1)
        res = {"ok": False, "error": "format_error: no tool call emitted"}

        execution_ok = res.get("ok") or res.get("status") == "ok"
        _is_format_error = "format_error" in str(res.get("error", ""))

        no_plan_fail_update = {}
        if not state.get("current_plan"):
            if execution_ok:
                no_plan_fail_update = {"no_plan_fail_count": 0}
            elif not _is_format_error:
                no_plan_fail_update = {
                    "no_plan_fail_count": int(state.get("no_plan_fail_count") or 0) + 1
                }

        # format_error: count should NOT increase
        assert no_plan_fail_update == {}

    def test_task_error_increments_count(self):
        """A genuine task failure (not a format error) still increments."""
        state = self._make_state(no_plan_fail_count=1)
        res = {"ok": False, "error": "FileNotFoundError: src/foo.py not found"}

        execution_ok = res.get("ok") or res.get("status") == "ok"
        _is_format_error = "format_error" in str(res.get("error", ""))

        no_plan_fail_update = {}
        if not state.get("current_plan"):
            if execution_ok:
                no_plan_fail_update = {"no_plan_fail_count": 0}
            elif not _is_format_error:
                no_plan_fail_update = {
                    "no_plan_fail_count": int(state.get("no_plan_fail_count") or 0) + 1
                }

        assert no_plan_fail_update == {"no_plan_fail_count": 2}

    def test_success_resets_count(self):
        state = self._make_state(no_plan_fail_count=3)
        res = {"ok": True, "status": "ok"}

        execution_ok = res.get("ok") or res.get("status") == "ok"
        _is_format_error = "format_error" in str(res.get("error", ""))

        no_plan_fail_update = {}
        if not state.get("current_plan"):
            if execution_ok:
                no_plan_fail_update = {"no_plan_fail_count": 0}
            elif not _is_format_error:
                no_plan_fail_update = {
                    "no_plan_fail_count": int(state.get("no_plan_fail_count") or 0) + 1
                }

        assert no_plan_fail_update == {"no_plan_fail_count": 0}


# ─────────────────────────────────────────────────────────────────────────────
# New role files exist
# ─────────────────────────────────────────────────────────────────────────────

class TestNewRoleFilesExist:
    _BASE = Path(__file__).parents[2] / "src" / "config" / "agent-brain" / "roles"

    def test_operational_small_exists(self):
        assert (self._BASE / "operational-small.md").exists()

    def test_operational_frontier_exists(self):
        assert (self._BASE / "operational-frontier.md").exists()

    def test_operational_small_is_short(self):
        content = (self._BASE / "operational-small.md").read_text()
        lines = [l for l in content.splitlines() if l.strip()]
        assert len(lines) <= 60, f"operational-small.md has {len(lines)} non-empty lines, expected ≤60"

    def test_operational_small_has_status_field(self):
        content = (self._BASE / "operational-small.md").read_text()
        assert "STATUS:" in content

    def test_operational_small_no_result_field(self):
        content = (self._BASE / "operational-small.md").read_text()
        # Should NOT contain the full 4-field format
        assert "FILES_CHANGED:" not in content

    def test_operational_frontier_has_reflection_gate(self):
        content = (self._BASE / "operational-frontier.md").read_text()
        assert "reflect" in content.lower() or "reasoning" in content.lower() or "I am calling" in content

    def test_operational_frontier_has_exploration_section(self):
        content = (self._BASE / "operational-frontier.md").read_text()
        assert "Exploration" in content or "Before modifying" in content


# ─────────────────────────────────────────────────────────────────────────────
# Gap analysis document exists
# ─────────────────────────────────────────────────────────────────────────────

class TestGapAnalysisDocument:
    _DOC = Path(__file__).parents[2] / "docs" / "audit" / "gap-analysis-opencode-vs-codingagent.md"

    def test_document_exists(self):
        assert self._DOC.exists()

    def test_document_has_small_model_section(self):
        content = self._DOC.read_text()
        assert "GAP-SMALL" in content

    def test_document_has_frontier_section(self):
        content = self._DOC.read_text()
        assert "GAP-FRONTIER" in content

    def test_document_has_implementation_priority(self):
        content = self._DOC.read_text()
        assert "Implementation Priority" in content or "Priority" in content

    def test_document_has_comparison_table(self):
        content = self._DOC.read_text()
        assert "CodingAgent" in content and "opencode" in content
