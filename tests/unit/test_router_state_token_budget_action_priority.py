"""
Regression tests for Audit Vol10 fixes and token budget / memory compaction.

CF-6: Token budget max_tokens uses realistic default (32,768), not self-calibrating
HR-2: distill_context returns compacted history; memory_update_node uses it
HR-8: start_new_task() clears PreviewService.pending_previews
HR-9: start_new_task() clears stale delegations
ME-3: memory_update_node uses distill_context result for analysis_summary
"""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# CF-6: Token budget baseline
# ---------------------------------------------------------------------------


class TestTokenBudgetRealisticMinimumDefault:
    def test_initial_max_tokens_is_not_6000(self):
        """get_budget should use ≥32,768 as default, not 6,000."""
        from src.core.orchestration.token_budget import TokenBudgetMonitor

        monitor = TokenBudgetMonitor()
        budget = monitor.get_budget("test-session-cf6")
        assert budget.max_tokens >= 32_768, (
            f"Initial max_tokens should be ≥32,768 (got {budget.max_tokens}). "
            "CF-6: small baseline makes usage_ratio always ~1.0"
        )

    def test_check_budget_does_not_grow_max_tokens_to_current_usage(self):
        """check_budget must not set max_tokens = total_raw (self-calibration bug)."""
        from src.core.orchestration.token_budget import TokenBudgetMonitor

        monitor = TokenBudgetMonitor()
        session_id = "test-cf6-grow"

        # Put some history in state
        big_message = {"role": "user", "content": "x" * 4000}  # ~1000 tokens est.
        state = {
            "session_id": session_id,
            "history": [big_message] * 5,  # ~5,000 tokens
            "_p2p_context": [],
            "rounds": 1,
        }

        monitor.check_budget(state)
        budget = monitor.get_budget(session_id)

        # Bug: max_tokens would be set to total_raw (~5000) causing ratio=1.0
        # Fix: max_tokens must remain at the realistic default (≥32,768)
        assert budget.max_tokens >= 32_768, (
            f"max_tokens was calibrated to current usage ({budget.max_tokens}). "
            "CF-6: this makes usage_ratio always 1.0 → compaction every turn."
        )

    def test_usage_ratio_is_reasonable_for_small_history(self):
        """With a small history, usage_ratio should be well below 0.85 threshold."""
        from src.core.orchestration.token_budget import TokenBudgetMonitor

        monitor = TokenBudgetMonitor()
        session_id = "test-cf6-ratio"
        state = {
            "session_id": session_id,
            "history": [{"role": "user", "content": "hello"}] * 3,
            "_p2p_context": [],
            "rounds": 1,
        }
        monitor.check_budget(state)
        budget = monitor.get_budget(session_id)
        assert budget.usage_ratio < 0.85, (
            f"Small history should have low usage_ratio (got {budget.usage_ratio:.2f}). "
            "CF-6: self-calibrating baseline inflated this to 1.0"
        )


# ---------------------------------------------------------------------------
# HR-2: distill_context returns compacted history; memory_update_node uses it
# ---------------------------------------------------------------------------


class TestDistillContextCompactsHistoryAtThreshold:
    def test_distill_context_returns_compacted_history_key_at_50_msgs(self):
        """distill_context must return '_compacted_history' when len(messages) >= 50."""
        from src.core.memory.distiller import distill_context

        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(55)
        ]

        # TASK-07 changed the trigger from message count to token count (threshold=6000).
        # Short test messages ("msg 0" etc.) produce ~65 estimated tokens — below threshold.
        # Patch _estimate_tokens to exceed the threshold so compaction is triggered.
        with patch("src.core.memory.distiller._estimate_tokens", return_value=7000):
            with patch(
                "src.core.memory.distiller.compact_messages_to_prose",
                return_value="Compacted summary text",
            ):
                with patch(
                    "src.core.memory.distiller._call_llm_sync",
                    return_value='{"current_task":"t","current_state":"s","next_step":"n"}',
                ):
                    result = distill_context(messages)

        assert "_compacted_history" in result, (
            "distill_context must include '_compacted_history' in its return dict "
            "when len(messages) >= 50 (HR-2 fix)"
        )
        compacted = result["_compacted_history"]
        assert isinstance(compacted, list)
        assert len(compacted) < 55, "Compacted history should be shorter than original"

    def test_distill_context_no_compacted_history_below_50_msgs(self):
        """distill_context must NOT include '_compacted_history' for short history."""
        from src.core.memory.distiller import distill_context

        messages = [{"role": "user", "content": f"msg {i}"} for i in range(10)]

        with patch(
            "src.core.memory.distiller._call_llm_sync",
            return_value='{"current_task":"t","current_state":"s","next_step":"n"}',
        ):
            result = distill_context(messages)

        assert "_compacted_history" not in result, (
            "distill_context must not return '_compacted_history' for < 50 messages"
        )


class TestMemoryUpdateNodeAppliesCompactedHistory:
    @staticmethod
    def _make_messages(n: int):
        return [{"role": "user", "content": f"msg {i}"} for i in range(n)]

    @staticmethod
    def _make_state(n: int, tmp_path: Path) -> Dict[str, Any]:
        return {
            "working_dir": str(tmp_path),
            "history": TestMemoryUpdateNodeAppliesCompactedHistory._make_messages(n),
            "_should_distill": True,
            "_force_compact": False,
            "evaluation_result": "complete",
            "task": "test task",
            "session_id": "test",
        }

    def test_memory_update_node_returns_compacted_history(self, tmp_path):
        """memory_update_node must return compacted history when CompactionService compacts."""
        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )
        from src.core.memory.compaction_service import CompactionResult

        state = self._make_state(55, tmp_path)
        config = {"configurable": {"orchestrator": None}}

        compact_msgs = [{"role": "system", "content": "Session Summary:\nCompacted"}]
        ok_result = CompactionResult(
            success=True,
            compacted_history=compact_msgs,
            method="deterministic",
        )

        fake_service = MagicMock()
        fake_service.should_compact.return_value = True
        fake_service.compact.return_value = ok_result

        with patch(
            "src.core.memory.compaction_service.CompactionService",
            return_value=fake_service,
        ):
            with patch(
                "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
                return_value={},
            ):
                result = asyncio.run(memory_update_node(state, config))  # type: ignore[arg-type]

        assert "history" in result, (
            "memory_update_node must return 'history' when CompactionService compacts"
        )
        assert result["history"] == compact_msgs

    def test_memory_update_node_returns_force_compact_false(self, tmp_path):
        """memory_update_node always returns _force_compact: False."""
        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )

        state = self._make_state(5, tmp_path)
        config = {"configurable": {"orchestrator": None}}

        with patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            return_value={},
        ):
            result = asyncio.run(memory_update_node(state, config))  # type: ignore[arg-type]

        assert result.get("_force_compact") is False


# ---------------------------------------------------------------------------
# HR-8 + HR-9: start_new_task clears PreviewService and delegations
# ---------------------------------------------------------------------------


class TestOrchestratorStartNewTaskClearsStaleState:
    def test_start_new_task_clears_preview_service(self, tmp_path):
        """start_new_task() must clear PreviewService.pending_previews."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = Orchestrator(working_dir=str(tmp_path))

        # Inject a stale preview entry
        try:
            from src.core.orchestration.preview_service import PreviewService

            svc = PreviewService.get_instance()
            svc.pending_previews["stale-preview-123"] = MagicMock()
            assert "stale-preview-123" in svc.pending_previews

            orch.start_new_task()

            assert "stale-preview-123" not in svc.pending_previews, (
                "start_new_task() must clear PreviewService.pending_previews. "
                "HR-8: stale previews from prior task block new task."
            )
        except ImportError:
            pass  # PreviewService not available in test env — skip

    def test_start_new_task_clears_pending_delegations(self, tmp_path):
        """start_new_task() must reset _pending_delegations."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = Orchestrator(working_dir=str(tmp_path))

        # Inject stale delegations
        orch._pending_delegations = [{"role": "analyst", "task": "stale"}]
        assert len(orch._pending_delegations) == 1

        orch.start_new_task()

        assert orch._pending_delegations == [], (
            "start_new_task() must clear _pending_delegations. "
            "HR-9: stale delegations cause spurious delegation on next task."
        )


# ---------------------------------------------------------------------------
# ME-3: memory_update_node uses distill_context result for analysis_summary
# ---------------------------------------------------------------------------


class TestMemoryUpdateNodeInjectsDistilledAnalysisSummary:
    def test_memory_update_node_returns_analysis_summary_from_distilled(self, tmp_path):
        """memory_update_node must return 'analysis_summary' from distilled current_state."""
        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )

        state = {
            "working_dir": str(tmp_path),
            "history": [{"role": "user", "content": f"m{i}"} for i in range(5)],
            "_should_distill": True,
            "_force_compact": False,
            "evaluation_result": "complete",
            "task": "test",
            "session_id": "me3-test",
        }
        config = {"configurable": {"orchestrator": None}}

        distilled_return = {
            "current_task": "test task",
            "current_state": "All steps completed successfully.",
            "next_step": "Done",
        }

        with patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            return_value=distilled_return,
        ):
            result = asyncio.run(memory_update_node(state, config))  # type: ignore[arg-type]

        assert "analysis_summary" in result, (
            "memory_update_node must return 'analysis_summary' from distilled current_state. "
            "ME-3: distilled state never fed back to agent context."
        )
        assert result["analysis_summary"] == "All steps completed successfully.", (
            f"analysis_summary has wrong value: {result.get('analysis_summary')!r}"
        )
