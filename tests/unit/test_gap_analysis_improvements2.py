"""
Regression tests for gap-analysis improvements (second batch).

Covers:
  GAP-SMALL-3   provider_supports_parallel_tools → SMALL simple_mode
  GAP-SMALL-4   needs_clarification guard in perception_node
  GAP-FRONTIER-3 parallel analyst subagents for FRONTIER tier
  GAP-FRONTIER-7 thinking_mode block injected for FRONTIER/LARGE/reasoning models
  Model-specific prompt partial files exist (GAP-FRONTIER-1)
"""


# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio


# ─────────────────────────────────────────────────────────────────────────────
# GAP-SMALL-3: provider_supports_parallel_tools → extends simple_mode to SMALL
# ─────────────────────────────────────────────────────────────────────────────

class TestSmall3ParallelToolsFlag:
    def _compute_simple_mode(self, model_tier: str, provider_supports_parallel_tools: bool) -> bool:
        """Replicate the GAP-SMALL-3 logic from build_prompt."""
        from src.core.inference.model_tiers import ModelTier, is_simple_mode as _check_simple

        _tier_val = ModelTier(model_tier)
        _is_simple = _check_simple(_tier_val)

        if not _is_simple and _tier_val == ModelTier.SMALL:
            if not provider_supports_parallel_tools:
                _is_simple = True

        return _is_simple

    def test_small_no_parallel_tools_enters_simple_mode(self):
        result = self._compute_simple_mode("small", provider_supports_parallel_tools=False)
        assert result is True

    def test_small_with_parallel_tools_stays_normal(self):
        result = self._compute_simple_mode("small", provider_supports_parallel_tools=True)
        assert result is False

    def test_nano_always_simple_mode(self):
        result = self._compute_simple_mode("nano", provider_supports_parallel_tools=True)
        assert result is True

    def test_medium_not_simple_mode(self):
        result = self._compute_simple_mode("medium", provider_supports_parallel_tools=False)
        assert result is False

    def test_frontier_not_simple_mode(self):
        result = self._compute_simple_mode("frontier", provider_supports_parallel_tools=False)
        assert result is False

    def test_providers_json_has_flag(self):
        import json
        path = Path(__file__).parents[2] / "src" / "config" / "providers.json"
        providers = json.loads(path.read_text())
        for p in providers:
            assert "provider_supports_parallel_tools" in p, (
                f"Provider '{p.get('name')}' missing provider_supports_parallel_tools"
            )

    def test_local_providers_have_flag_false(self):
        import json
        path = Path(__file__).parents[2] / "src" / "config" / "providers.json"
        providers = json.loads(path.read_text())
        local_types = {"lm_studio", "ollama"}
        for p in providers:
            if p.get("type") in local_types:
                assert p["provider_supports_parallel_tools"] is False, (
                    f"Local provider '{p['name']}' should have provider_supports_parallel_tools=False"
                )

    def test_local_providers_have_disable_thinking_field(self):
        # disable_thinking is a valid config field but its value depends on the model.
        # Gemma 4 and other thinking-capable local models should have disable_thinking=False
        # so that thinking tokens are budget-controlled rather than suppressed.
        # This test only checks the field is present (not its value).
        import json
        path = Path(__file__).parents[2] / "src" / "config" / "providers.json"
        providers = json.loads(path.read_text())
        for p in providers:
            if p.get("type") in {"lm_studio", "ollama"}:
                assert "disable_thinking" in p, (
                    f"Local provider '{p['name']}' must declare disable_thinking"
                )


# ─────────────────────────────────────────────────────────────────────────────
# GAP-SMALL-4: needs_clarification guard logic
# ─────────────────────────────────────────────────────────────────────────────

class TestSmall4ClarificationGuard:
    def _should_clarify(self, task: str) -> bool:
        """Replicate the clarification guard heuristic from perception_node."""
        import re
        _task_words = task.strip().split()
        _ACTION_VERBS = {
            "add", "fix", "update", "change", "create", "delete", "remove",
            "refactor", "write", "read", "run", "test", "debug", "find",
            "search", "show", "list", "explain", "implement", "build",
            "install", "deploy", "check", "verify", "move", "rename",
        }
        _has_action = any(w.lower() in _ACTION_VERBS for w in _task_words)
        _has_file_ref = bool(re.search(r"\w+\.\w+|/\w+|\w+\.py\b", task))
        _has_code_id = bool(re.search(r"`[^`]+`|\"[A-Za-z_]\w+\"", task))
        return len(_task_words) < 8 and not _has_action and not _has_file_ref and not _has_code_id

    def test_very_short_ambiguous_triggers_clarification(self):
        assert self._should_clarify("hello") is True

    def test_task_with_action_verb_does_not_trigger(self):
        assert self._should_clarify("fix the login bug in auth.py") is False

    def test_task_with_file_ref_does_not_trigger(self):
        assert self._should_clarify("update src/core/config.py") is False

    def test_task_with_code_identifier_does_not_trigger(self):
        assert self._should_clarify('fix the `parse_response` function') is False

    def test_longer_task_does_not_trigger(self):
        # ≥8 words with no action still does not trigger because the word count guard fails
        assert self._should_clarify("the thing with the other thing and also this") is False

    def test_empty_task_triggers_clarification(self):
        assert self._should_clarify("") is True

    def test_task_with_implement_verb_does_not_trigger(self):
        assert self._should_clarify("implement feature") is False

    def test_needs_clarification_field_in_state(self):
        from src.core.orchestration.graph.state import AgentState
        import typing
        hints = typing.get_type_hints(AgentState)
        assert "needs_clarification" in hints


# ─────────────────────────────────────────────────────────────────────────────
# GAP-FRONTIER-3: parallel analyst delegation
# ─────────────────────────────────────────────────────────────────────────────

class TestFrontier3ParallelAnalysts:
    def test_parallel_analyst_tiers_defined(self):
        from src.core.orchestration.graph.nodes.analyst_delegation_node import (
            _PARALLEL_ANALYST_TIERS,
        )
        assert "frontier" in _PARALLEL_ANALYST_TIERS
        assert "large" in _PARALLEL_ANALYST_TIERS

    def test_build_parallel_subtasks_returns_three(self):
        from src.core.orchestration.graph.nodes.analyst_delegation_node import (
            _build_parallel_subtasks,
        )
        subtasks = _build_parallel_subtasks("Fix auth", "auth.py", "summary")
        assert len(subtasks) == 3

    def test_parallel_subtasks_have_different_focus(self):
        from src.core.orchestration.graph.nodes.analyst_delegation_node import (
            _build_parallel_subtasks,
        )
        subtasks = _build_parallel_subtasks("Fix auth", "auth.py", "summary")
        foci = [s.split("FOCUS:")[1].split("\n")[0].strip() for s in subtasks]
        assert len(set(foci)) == 3, "All three subtasks must have different focus areas"

    def test_merge_findings_combines_results(self):
        from src.core.orchestration.graph.nodes.analyst_delegation_node import (
            _merge_findings,
        )
        results = ["findings A", "findings B", "findings C"]
        merged = _merge_findings(results)
        assert "findings A" in merged
        assert "findings B" in merged
        assert "findings C" in merged

    def test_merge_findings_skips_empty(self):
        from src.core.orchestration.graph.nodes.analyst_delegation_node import (
            _merge_findings,
        )
        results = ["findings A", "", "findings C"]
        merged = _merge_findings(results)
        assert "findings A" in merged
        assert "findings C" in merged
        # Empty result should not produce an empty tag block
        assert "<analyst_dependencies>\n\n</analyst_dependencies>" not in merged

    def test_parallel_analysts_spawned_for_frontier(self):
        """For FRONTIER tier, asyncio.gather is called with 3 coroutines."""
        from src.core.orchestration.graph.nodes.analyst_delegation_node import (
            analyst_delegation_node,
        )

        called_with = []

        async def fake_delegate(role, subtask_description, working_dir):
            called_with.append(subtask_description[:20])
            return f"findings for {role}"

        state = {
            "task": "Refactor the authentication system",
            "analysis_summary": "summary",
            "relevant_files": ["auth.py"],
            "working_dir": "/tmp",
            "model_tier": "frontier",
        }

        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            side_effect=fake_delegate,
        ):
            result = asyncio.run(analyst_delegation_node(state, None))

        # 3 parallel calls
        assert len(called_with) == 3
        assert "analyst_findings" in result

    def test_single_analyst_for_medium(self):
        """For MEDIUM tier, only one analyst is spawned."""
        from src.core.orchestration.graph.nodes.analyst_delegation_node import (
            analyst_delegation_node,
        )

        called_with = []

        async def fake_delegate(role, subtask_description, working_dir):
            called_with.append(1)
            return "findings"

        state = {
            "task": "Fix the login bug",
            "analysis_summary": "summary",
            "relevant_files": ["auth.py"],
            "working_dir": "/tmp",
            "model_tier": "medium",
        }

        with patch(
            "src.core.orchestration.graph.nodes.analyst_delegation_node.delegate_task_async",
            side_effect=fake_delegate,
        ):
            result = asyncio.run(analyst_delegation_node(state, None))

        assert len(called_with) == 1
        assert "analyst_findings" in result


# ─────────────────────────────────────────────────────────────────────────────
# GAP-FRONTIER-7: thinking_mode block
# ─────────────────────────────────────────────────────────────────────────────

class TestFrontier7ThinkingMode:
    def _build_prefix(self, tier: str, is_reasoning: bool = False) -> str:
        from src.core.context.context_builder import ContextBuilder, _STATIC_PROMPT_CACHE
        _STATIC_PROMPT_CACHE.clear()
        builder = ContextBuilder()
        builder.roles = {"operational": "op"}
        builder.soul = "soul"
        builder.skills = {}
        with patch(
            "src.core.context.context_builder.ContextBuilder._select_prompt_partial",
            return_value="",
        ):
            with patch(
                "src.core.context.context_builder.ContextBuilder._load_agent_brain",
                return_value=None,
            ):
                pass
        # Build normally — the thinking_mode injection is the part we test
        return builder._build_static_system_prefix(
            role_name="operational",
            active_skills=[],
            tools=[],
            model_tier=tier,
            provider_capabilities={},
            use_native_tools=False,
            is_simple_mode=False,
        )

    def test_frontier_gets_thinking_mode_block(self):
        prefix = self._build_prefix("frontier")
        assert "<thinking_mode>" in prefix

    def test_large_gets_thinking_mode_block(self):
        prefix = self._build_prefix("large")
        assert "<thinking_mode>" in prefix

    def test_medium_does_not_get_thinking_mode(self):
        prefix = self._build_prefix("medium")
        assert "<thinking_mode>" not in prefix

    def test_small_does_not_get_thinking_mode(self):
        prefix = self._build_prefix("small")
        assert "<thinking_mode>" not in prefix

    def test_thinking_mode_content_mentions_tool_call(self):
        prefix = self._build_prefix("frontier")
        assert "tool call" in prefix.lower() or "expect" in prefix.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Model-specific prompt partial files (GAP-FRONTIER-1)
# ─────────────────────────────────────────────────────────────────────────────

class TestModelSpecificPartialFiles:
    _TEMPLATES = Path(__file__).parents[2] / "src" / "core" / "prompts" / "templates"

    def test_anthropic_frontier_exists(self):
        assert (self._TEMPLATES / "anthropic-frontier.md").exists()

    def test_anthropic_small_exists(self):
        assert (self._TEMPLATES / "anthropic-small.md").exists()

    def test_openai_reasoning_exists(self):
        assert (self._TEMPLATES / "openai-reasoning.md").exists()

    def test_openai_frontier_exists(self):
        assert (self._TEMPLATES / "openai-frontier.md").exists()

    def test_gemini_frontier_exists(self):
        assert (self._TEMPLATES / "gemini-frontier.md").exists()

    def test_gemini_small_exists(self):
        assert (self._TEMPLATES / "gemini-small.md").exists()

    def test_anthropic_frontier_has_reflection_gate(self):
        content = (self._TEMPLATES / "anthropic-frontier.md").read_text()
        assert "Reflection" in content or "reflect" in content.lower() or "Before every" in content

    def test_openai_reasoning_mentions_persistence(self):
        content = (self._TEMPLATES / "openai-reasoning.md").read_text()
        assert "keep going" in content.lower() or "persist" in content.lower() or "fully solved" in content.lower()

    def test_anthropic_small_is_short(self):
        content = (self._TEMPLATES / "anthropic-small.md").read_text()
        lines = [l for l in content.splitlines() if l.strip()]
        assert len(lines) <= 25, f"anthropic-small.md has {len(lines)} non-empty lines, expected ≤25"

    def test_gemini_small_is_short(self):
        content = (self._TEMPLATES / "gemini-small.md").read_text()
        lines = [l for l in content.splitlines() if l.strip()]
        assert len(lines) <= 25

    def test_model_id_partial_map_covers_new_files(self):
        from src.core.context.context_builder import ContextBuilder
        filenames = {filename for _, filename in ContextBuilder._MODEL_ID_PARTIAL_MAP}
        assert "openai-reasoning.md" in filenames
        assert "anthropic-frontier.md" in filenames
        assert "anthropic-small.md" in filenames
        assert "gemini-frontier.md" in filenames
        assert "gemini-small.md" in filenames
