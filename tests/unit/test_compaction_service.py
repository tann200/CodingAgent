"""Tests for P2-1: CompactionService — unified context compaction facade."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from src.core.memory.compaction_service import CompactionResult, CompactionService


def _make_history(n_messages: int = 5, content_len: int = 100) -> List[Dict[str, Any]]:
    content = "x" * content_len
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": content} for i in range(n_messages)]


# ---------------------------------------------------------------------------
# CompactionResult
# ---------------------------------------------------------------------------


class TestCompactionResult:
    def test_defaults(self):
        r = CompactionResult()
        assert r.success is False
        assert r.compacted_history == []
        assert r.method == "none"
        assert r.tokens_before == 0
        assert r.tokens_after == 0
        assert r.error == ""

    def test_custom_values(self):
        r = CompactionResult(
            success=True,
            compacted_history=[{"role": "system", "content": "summary"}],
            method="deterministic",
            tokens_before=500,
            tokens_after=50,
        )
        assert r.success is True
        assert len(r.compacted_history) == 1
        assert r.method == "deterministic"
        assert r.tokens_before == 500
        assert r.tokens_after == 50


# ---------------------------------------------------------------------------
# CompactionService — construction
# ---------------------------------------------------------------------------


class TestCompactionServiceConstruction:
    def test_basic_construction(self):
        history = _make_history(3)
        svc = CompactionService(history)
        assert svc is not None

    def test_history_is_copied(self):
        history = _make_history(2)
        svc = CompactionService(history)
        history.clear()
        # Service should still have its own copy
        result = svc.compact()
        assert result.success is True

    def test_prefer_deterministic_flag(self):
        svc = CompactionService([], prefer_deterministic=True)
        assert svc._prefer_deterministic is True


# ---------------------------------------------------------------------------
# CompactionService.should_compact()
# ---------------------------------------------------------------------------


class TestShouldCompact:
    def test_empty_history_below_threshold(self):
        svc = CompactionService([], compact_threshold=0.85)
        assert svc.should_compact(token_limit=100_000) is False

    def test_tiny_history_below_threshold(self):
        svc = CompactionService(_make_history(3, content_len=10))
        assert svc.should_compact(token_limit=100_000) is False

    def test_huge_history_above_threshold(self):
        # 1000 messages × 1000 chars = 1 000 000 chars ≈ 250 000 tokens
        svc = CompactionService(_make_history(1000, content_len=1000))
        assert svc.should_compact(token_limit=100_000) is True

    def test_threshold_respected(self):
        # 10 messages × 400 chars = 4 000 chars ≈ 1 000 tokens
        svc = CompactionService(
            _make_history(10, content_len=400),
            compact_threshold=0.5,
        )
        # Should be below 500 * 4 = 2000 chars threshold
        assert svc.should_compact(token_limit=1000) is True

    def test_should_compact_does_not_raise_on_auto_compactor_failure(self):
        """Fallback path must not raise even if auto_compactor is broken."""
        svc = CompactionService(_make_history(5, content_len=100))
        with patch(
            "src.core.memory.compaction_service.CompactionService._estimate_tokens",
            side_effect=RuntimeError("broken"),
        ):
            # Falls back to char heuristic — may return True or False, just must not raise
            try:
                result = svc.should_compact()
                assert isinstance(result, bool)
            except RuntimeError:
                pytest.fail("should_compact raised RuntimeError instead of returning bool")


# ---------------------------------------------------------------------------
# CompactionService.compact() — deterministic path
# ---------------------------------------------------------------------------


class TestCompactDeterministic:
    def test_empty_history_returns_success_no_method(self):
        svc = CompactionService([], prefer_deterministic=True)
        result = svc.compact()
        assert result.success is True
        assert result.method == "none"

    def test_non_empty_history_succeeds(self):
        svc = CompactionService(_make_history(5), prefer_deterministic=True)
        result = svc.compact()
        assert result.success is True
        assert result.method == "deterministic"
        assert isinstance(result.compacted_history, list)

    def test_tokens_before_populated(self):
        svc = CompactionService(_make_history(5, content_len=200), prefer_deterministic=True)
        result = svc.compact()
        assert result.tokens_before > 0

    def test_compacted_history_is_list_of_dicts(self):
        svc = CompactionService(_make_history(3), prefer_deterministic=True)
        result = svc.compact()
        for msg in result.compacted_history:
            assert isinstance(msg, dict)
            assert "role" in msg

    def test_never_raises(self):
        """compact() must never propagate exceptions."""
        svc = CompactionService(_make_history(3), prefer_deterministic=True)
        with patch(
            "src.core.memory.auto_compactor.compact_messages",
            side_effect=RuntimeError("crash"),
        ):
            result = svc.compact()
        assert isinstance(result, CompactionResult)
        assert result.success is False
        assert result.method == "error"
        assert "crash" in result.error


# ---------------------------------------------------------------------------
# CompactionService.compact() — LLM path with fallback
# ---------------------------------------------------------------------------


class TestCompactLLMWithFallback:
    def test_falls_back_to_deterministic_when_llm_fails(self):
        svc = CompactionService(_make_history(5), prefer_deterministic=False)
        with patch(
            "src.core.memory.distiller.distill_context",
            side_effect=RuntimeError("no LLM"),
        ):
            result = svc.compact()
        # Should have fallen back to deterministic
        assert result.success is True
        assert result.method == "deterministic"

    def test_uses_llm_when_available(self):
        fake_output = {
            "history": [{"role": "system", "content": "LLM summary"}],
            "summary": "LLM summary",
        }
        svc = CompactionService(_make_history(5), prefer_deterministic=False)
        with patch("src.core.memory.distiller.distill_context", return_value=fake_output):
            result = svc.compact()
        assert result.success is True
        assert result.method == "llm"
        assert any("LLM summary" in m.get("content", "") for m in result.compacted_history)

    def test_llm_result_with_only_summary_key(self):
        """distill_context may return only 'summary' (prose string) with no 'history'."""
        fake_output = {"summary": "A prose summary of the session."}
        svc = CompactionService(_make_history(5), prefer_deterministic=False)
        with patch("src.core.memory.distiller.distill_context", return_value=fake_output):
            result = svc.compact()
        assert result.success is True
        assert result.method == "llm"
        assert len(result.compacted_history) >= 1


# ---------------------------------------------------------------------------
# Event bus publishing
# ---------------------------------------------------------------------------


class TestEventBusPublishing:
    def test_publishes_compacted_event_on_success(self):
        bus = MagicMock()
        svc = CompactionService(_make_history(3), event_bus=bus, prefer_deterministic=True)
        svc.compact()
        bus.publish.assert_called_once()
        call_args = bus.publish.call_args
        assert call_args[0][0] == "context.compacted"

    def test_publishes_failure_event_on_error(self):
        bus = MagicMock()
        svc = CompactionService(_make_history(3), event_bus=bus, prefer_deterministic=True)
        with patch(
            "src.core.memory.auto_compactor.compact_messages",
            side_effect=RuntimeError("forced failure"),
        ):
            svc.compact()
        bus.publish.assert_called_once()
        call_args = bus.publish.call_args
        assert call_args[0][0] == "context.compact.failed"

    def test_no_event_published_for_empty_history(self):
        """Empty history returns method='none' — compaction never ran, no event."""
        bus = MagicMock()
        svc = CompactionService([], event_bus=bus)
        svc.compact()
        # compact() returns early for empty history before _publish_event
        bus.publish.assert_not_called()

    def test_event_bus_failure_does_not_propagate(self):
        bus = MagicMock()
        bus.publish.side_effect = RuntimeError("bus broken")
        svc = CompactionService(_make_history(3), event_bus=bus, prefer_deterministic=True)
        result = svc.compact()
        # Compaction itself should still succeed
        assert result.success is True

    def test_no_event_bus_works_silently(self):
        svc = CompactionService(_make_history(3), event_bus=None, prefer_deterministic=True)
        result = svc.compact()
        assert result.success is True


# ---------------------------------------------------------------------------
# P2-1: compact_context_impl routed through CompactionService
# ---------------------------------------------------------------------------


class TestCompactContextImpl:
    """compact_context_impl must now delegate to CompactionService."""

    def _make_orch(self, history=None, working_dir="/tmp/test"):
        orch = MagicMock()
        _hist = history if history is not None else _make_history(5)
        orch.msg_mgr.messages = _hist
        orch.working_dir = working_dir
        orch.event_bus = MagicMock()
        return orch

    def test_returns_true_on_success(self):
        from src.core.orchestration.orchestrator_helpers import compact_context_impl

        orch = self._make_orch()
        ok_result = CompactionResult(
            success=True,
            compacted_history=_make_history(2),
            method="deterministic",
            tokens_before=500,
            tokens_after=100,
        )
        with patch(
            "src.core.memory.compaction_service.CompactionService.compact",
            return_value=ok_result,
        ):
            result = compact_context_impl(orch)

        assert result is True

    def test_returns_false_when_no_history(self):
        from src.core.orchestration.orchestrator_helpers import compact_context_impl

        orch = self._make_orch(history=[])
        assert compact_context_impl(orch) is False

    def test_returns_false_on_compaction_failure(self):
        from src.core.orchestration.orchestrator_helpers import compact_context_impl

        orch = self._make_orch()
        fail_result = CompactionResult(
            success=False,
            method="error",
            error="LLM timeout",
        )
        with patch(
            "src.core.memory.compaction_service.CompactionService.compact",
            return_value=fail_result,
        ):
            result = compact_context_impl(orch)

        assert result is False

    def test_publishes_failure_event_on_exception(self):
        """If CompactionService itself raises, the failure event is still published."""
        from src.core.orchestration.orchestrator_helpers import compact_context_impl

        orch = self._make_orch()
        with patch(
            "src.core.memory.compaction_service.CompactionService",
            side_effect=RuntimeError("boom"),
        ):
            result = compact_context_impl(orch)

        assert result is False
        orch.event_bus.publish.assert_called_once()
        call_args = orch.event_bus.publish.call_args[0]
        assert call_args[0] == "context.compact.failed"

    def test_uses_compaction_service_not_distiller_directly(self):
        """After P2-1 fix, compact_context_impl must NOT import distill_context directly.
        Instead it must construct CompactionService. Verify that CompactionService.__init__
        is called with the correct history.
        """
        from src.core.orchestration.orchestrator_helpers import compact_context_impl

        history = _make_history(4)
        orch = self._make_orch(history=history)

        constructed_with: list = []

        class _TrackingService:
            def __init__(self, history, **kwargs):
                constructed_with.append(list(history))
                self._history = history

            def compact(self):
                return CompactionResult(
                    success=True,
                    compacted_history=self._history[:2],
                    method="deterministic",
                )

        with patch(
            "src.core.memory.compaction_service.CompactionService",
            _TrackingService,
        ):
            compact_context_impl(orch)

        assert len(constructed_with) == 1
        assert constructed_with[0] == history
