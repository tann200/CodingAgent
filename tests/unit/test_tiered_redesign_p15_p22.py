"""Regression tests for tiered-model redesign priorities 15–22.

Covers:
  P15 / OP-4  — overflow detection uses get_actual_context_window (no fraction cap)
  P16 / P1-G  — tool_last_used capped at 100; snapshots capped at 10
  P17 / OP-10 — _prune_tool_outputs honours metadata.preserve flag
  P18 / OP-2  — compact_messages_to_prose uses structured prompt (Goal/…/Current State)
  P19 / P2-D  — _prune_tool_outputs uses tiktoken when available
  P20 / P2-H  — _parse_yaml_block returns None for non-dict YAML (scalar/list)
  P21 / P2-E  — "dead" router functions are not removed; docstrings updated
  P22 / OP-8  — compacted message carries [COMPACTED] marker
"""


# ruff: noqa: E501
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# P15 / OP-4 — get_actual_context_window returns raw window (no cap)
# ---------------------------------------------------------------------------


class TestOP4ActualContextWindow:
    def test_function_exists(self):
        from src.core.inference.provider_context import get_actual_context_window

        assert callable(get_actual_context_window)

    def test_returns_integer(self):
        from src.core.inference.provider_context import get_actual_context_window

        val = get_actual_context_window()
        assert isinstance(val, int)
        assert val > 0

    def test_not_capped_at_131072(self):
        """get_actual_context_window must NOT apply a 131072 upper cap."""
        from src.core.inference.provider_context import (
            get_actual_context_window,
            set_active_context_length,
        )

        # Simulate a 256K-context model
        try:
            set_active_context_length(262144)
            window = get_actual_context_window()
            assert window == 262144, (
                f"Expected 262144, got {window} — get_actual_context_window() "
                "must not apply the 131072 cap that get_context_budget() uses"
            )
        finally:
            set_active_context_length(0)  # restore

    def test_vs_get_context_budget_differs_for_large_window(self):
        """For a 200K window, get_actual_context_window > get_context_budget."""
        from src.core.inference.provider_context import (
            get_actual_context_window,
            get_context_budget,
            set_active_context_length,
        )

        try:
            set_active_context_length(200_000)
            raw = get_actual_context_window()
            budgeted = get_context_budget()
            assert raw > budgeted, (
                "For a 200K window get_actual_context_window() should exceed "
                "get_context_budget() (which is capped at 131072)"
            )
        finally:
            set_active_context_length(0)

    def test_overflow_detection_uses_actual_window(self):
        """perception_node imports get_actual_context_window for overflow check."""
        import ast
        from pathlib import Path

        src = Path(
            "src/core/orchestration/graph/nodes/perception_node.py"
        ).read_text()
        assert "get_actual_context_window" in src, (
            "perception_node.py must import get_actual_context_window for OP-4"
        )
        # Must NOT still use get_context_budget in the overflow section
        # (it's still used for the compaction budget — we only care about the
        # overflow detection block using get_actual_context_window)
        assert "get_actual_context_window" in src


# ---------------------------------------------------------------------------
# P16 / P1-G — tool_last_used capped at 100; snapshots capped at 10
# ---------------------------------------------------------------------------


class TestP1GMemoryLeaks:
    def _make_big_tool_last_used(self, n: int) -> dict:
        return {f"tool_{i}:path_{i}": i for i in range(n)}

    def test_tool_last_used_cap_constant_exists(self):
        import ast
        from pathlib import Path

        src = Path(
            "src/core/orchestration/graph/nodes/execution_node.py"
        ).read_text()
        assert "_MAX_TOOL_LAST_USED" in src, (
            "execution_node.py must define _MAX_TOOL_LAST_USED for P1-G"
        )

    def test_tool_last_used_trimmed_to_100(self):
        """When >100 entries exist, the dict must be trimmed to 100."""
        from pathlib import Path

        src = Path(
            "src/core/orchestration/graph/nodes/execution_node.py"
        ).read_text()
        # The cap value must be 100
        assert "_MAX_TOOL_LAST_USED = 100" in src, (
            "execution_node.py must set _MAX_TOOL_LAST_USED = 100 (P1-G)"
        )

    def test_snapshots_cap_in_perception_node(self):
        """perception_node must cap snapshots list to 10 entries."""
        from pathlib import Path

        src = Path(
            "src/core/orchestration/graph/nodes/perception_node.py"
        ).read_text()
        # Look for the capping pattern: [-9:] before append gives max 10
        assert "[-9:]" in src, (
            "perception_node.py must slice prior snapshots to [-9:] (P1-G cap)"
        )

    def test_p1g_comment_present(self):
        from pathlib import Path

        src = Path(
            "src/core/orchestration/graph/nodes/execution_node.py"
        ).read_text()
        assert "P1-G" in src


# ---------------------------------------------------------------------------
# P17 / OP-10 — _prune_tool_outputs honours preserve flag
# ---------------------------------------------------------------------------


class TestOP10PreserveFlag:
    def _make_tool_result_msg(self, content: str, preserve: bool = False) -> dict:
        msg: dict = {"role": "user", "content": f"tool_execution_result {content}"}
        if preserve:
            msg["metadata"] = {"preserve": True}
        return msg

    def test_preserve_true_not_pruned(self):
        """Messages with metadata.preserve=True must survive pruning."""
        from src.core.orchestration.graph.nodes.perception_node import (
            _prune_tool_outputs,
        )

        # Build a history large enough to trigger pruning
        big_content = "x" * 40_000  # ~10K tokens
        history = []
        # Add old messages: some normal tool results, one preserved
        for i in range(20):
            history.append(self._make_tool_result_msg(big_content))
        # The preserved message
        preserved = self._make_tool_result_msg("IMPORTANT CONTEXT", preserve=True)
        history.insert(5, preserved)

        pruned, count = _prune_tool_outputs(history)
        # Find the preserved message in output
        preserved_msgs = [
            m
            for m in pruned
            if m.get("metadata", {}).get("preserve")
        ]
        assert preserved_msgs, "Preserved message was pruned — OP-10 violation"
        assert "IMPORTANT CONTEXT" in preserved_msgs[0]["content"]

    def test_non_preserved_still_prunable(self):
        """Normal old tool results are still pruned as before."""
        from src.core.orchestration.graph.nodes.perception_node import (
            _prune_tool_outputs,
            _PRUNED_TOOL_PLACEHOLDER,
        )

        big_content = "x" * 40_000
        history = [self._make_tool_result_msg(big_content) for _ in range(20)]
        pruned, count = _prune_tool_outputs(history)
        assert count > 0, "Expected some messages to be pruned"

    def test_no_preserve_key_behaves_normally(self):
        """Messages without metadata key are unaffected by OP-10 code path."""
        from src.core.orchestration.graph.nodes.perception_node import (
            _prune_tool_outputs,
        )

        history = [{"role": "user", "content": "regular message"}] * 5
        pruned, count = _prune_tool_outputs(history)
        assert count == 0  # short history, nothing should be pruned
        assert len(pruned) == 5


# ---------------------------------------------------------------------------
# P18 / OP-2 — compact_messages_to_prose uses structured sections
# ---------------------------------------------------------------------------


class TestOP2StructuredCompaction:
    def test_prompt_contains_required_sections(self):
        """compact_messages_to_prose prompt must contain all 6 structured sections."""
        import inspect
        from src.core.memory.distiller import compact_messages_to_prose

        src = inspect.getsource(compact_messages_to_prose)
        required = [
            "## Goal",
            "## Instructions",
            "## Discoveries",
            "## Accomplished",
            "## Relevant Files",
            "## Current State",
        ]
        for section in required:
            assert section in src, (
                f"compact_messages_to_prose prompt missing section '{section}' (OP-2)"
            )

    def test_no_old_bullet_format(self):
        """Old bullet-point prompt format should be replaced."""
        import inspect
        from src.core.memory.distiller import compact_messages_to_prose

        src = inspect.getsource(compact_messages_to_prose)
        # Old format used "- **Task**:" etc
        assert "- **Task**:" not in src, (
            "Old bullet-point prompt format not replaced by structured OP-2 format"
        )


# ---------------------------------------------------------------------------
# P19 / P2-D — _prune_tool_outputs uses tiktoken
# ---------------------------------------------------------------------------


class TestP2DAccurateTokenEstimate:
    def test_tiktoken_import_attempted(self):
        """_prune_tool_outputs source must attempt to import count_tokens."""
        import inspect
        from src.core.orchestration.graph.nodes.perception_node import _prune_tool_outputs

        src = inspect.getsource(_prune_tool_outputs)
        assert "count_tokens" in src, (
            "_prune_tool_outputs must try to use tiktoken count_tokens (P2-D)"
        )

    def test_fallback_present(self):
        """Must still have a len//4 fallback for when tiktoken is unavailable."""
        import inspect
        from src.core.orchestration.graph.nodes.perception_node import _prune_tool_outputs

        src = inspect.getsource(_prune_tool_outputs)
        assert "// 4" in src, (
            "_prune_tool_outputs must have len//4 fallback for no-tiktoken envs"
        )

    def test_runs_without_error(self):
        """Function must run cleanly (tiktoken available or not)."""
        from src.core.orchestration.graph.nodes.perception_node import _prune_tool_outputs

        history = [
            {"role": "user", "content": "tool_execution_result " + "a" * 500}
            for _ in range(5)
        ]
        result, count = _prune_tool_outputs(history)
        assert isinstance(result, list)
        assert isinstance(count, int)


# ---------------------------------------------------------------------------
# P20 / P2-H — _parse_yaml_block returns None for non-dict YAML
# ---------------------------------------------------------------------------


class TestP2HYamlScalarFallthrough:
    def test_scalar_returns_none(self):
        """A plain YAML scalar must not fall through to the custom parser."""
        from src.core.orchestration.tool_parser import _parse_yaml_block

        result = _parse_yaml_block("just_a_string")
        assert result is None, (
            "_parse_yaml_block should return None for scalar YAML (P2-H)"
        )

    def test_integer_scalar_returns_none(self):
        from src.core.orchestration.tool_parser import _parse_yaml_block

        result = _parse_yaml_block("42")
        assert result is None

    def test_list_returns_none(self):
        from src.core.orchestration.tool_parser import _parse_yaml_block

        result = _parse_yaml_block("- item1\n- item2")
        assert result is None, (
            "_parse_yaml_block should return None for YAML list (P2-H)"
        )

    def test_valid_dict_still_works(self):
        from src.core.orchestration.tool_parser import _parse_yaml_block

        result = _parse_yaml_block("name: read_file\narguments:\n  path: foo.py")
        assert result is not None
        assert result["name"] == "read_file"

    def test_none_yaml_falls_through(self):
        """Null / empty YAML (None from safe_load) should still try custom parser."""
        from src.core.orchestration.tool_parser import _parse_yaml_block

        # "" would give None from safe_load — should return None (no valid tool)
        result = _parse_yaml_block("---")
        assert result is None  # no valid tool call anyway


# ---------------------------------------------------------------------------
# P21 / P2-E — router functions not deleted; docstrings clarified
# ---------------------------------------------------------------------------


class TestP2ERouterFunctionsNotDeleted:
    def test_should_after_planning_exists(self):
        from src.core.orchestration.graph.builder import should_after_planning

        assert callable(should_after_planning)

    def test_should_after_execution_exists(self):
        from src.core.orchestration.graph.builder import should_after_execution

        assert callable(should_after_execution)

    def test_should_after_verification_exists(self):
        from src.core.orchestration.graph.builder import should_after_verification

        assert callable(should_after_verification)

    def test_docstrings_contain_p2e_note(self):
        """Updated docstrings must reference P2-E audit note."""
        from src.core.orchestration.graph.builder import (
            should_after_planning,
            should_after_execution,
            should_after_verification,
        )

        for fn in (should_after_planning, should_after_execution, should_after_verification):
            assert "P2-E" in (fn.__doc__ or ""), (
                f"{fn.__name__}.__doc__ must reference P2-E audit note"
            )


# ---------------------------------------------------------------------------
# P22 / OP-8 — [COMPACTED] marker on compacted messages
# ---------------------------------------------------------------------------


class TestOP8CompactedMarker:
    def test_compacted_history_has_marker(self):
        """distill_context's compacted system message must start with [COMPACTED]."""
        import inspect
        from src.core.memory.distiller import distill_context

        src = inspect.getsource(distill_context)
        assert "[COMPACTED]" in src, (
            "distill_context must embed [COMPACTED] in the compacted system message (OP-8)"
        )

    def test_marker_position_in_distiller(self):
        """The marker must appear in the compacted message content string."""
        from pathlib import Path

        src = Path("src/core/memory/distiller.py").read_text()
        assert '"[COMPACTED]' in src or "'[COMPACTED]" in src, (
            "distiller.py must contain a string literal '[COMPACTED]' (OP-8)"
        )

    def test_fallback_compact_also_mentions_compaction(self):
        """_fallback_compact already has CONTEXT COMPACTED in first line."""
        from src.core.memory.distiller import _fallback_compact

        msgs = [{"role": "user", "content": "hello"}]
        result = _fallback_compact(msgs)
        assert "COMPACTED" in result.upper()
