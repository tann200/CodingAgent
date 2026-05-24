"""Tests for perception_node auto-compaction via CompactionService (P2-1).

These tests verify that:
- _run_auto_compaction delegates to CompactionService
- Cooldown logic prevents back-to-back compactions
- Events are published when compaction runs
- Graceful no-op when CompactionService is unavailable
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


def _make_state(rounds: int = 0, session_id: str = "s1", compaction_last_round=None):
    state = {"rounds": rounds, "session_id": session_id}
    if compaction_last_round is not None:
        state["_compaction_last_round"] = compaction_last_round
    return state


def test_auto_compaction_skips_when_service_unavailable():
    """When CompactionService cannot be imported, _run_auto_compaction returns
    the original history unchanged without raising.
    """
    from src.core.orchestration.graph.nodes.perception_node import _run_auto_compaction

    hist = [{"role": "assistant", "content": "a"}] * 10
    state = _make_state()
    adapter = MagicMock()
    orc = MagicMock()

    # Simulate CompactionService import failure
    with patch.dict(
        sys.modules,
        {"src.core.memory.compaction_service": None},
    ):
        new_hist, new_compacted = _run_auto_compaction(hist, adapter, orc, state)

    assert new_hist == hist
    assert new_compacted is None


def test_auto_compaction_respects_cooldown():
    """When compaction ran recently (gap < 3 rounds), skip compaction."""
    from src.core.orchestration.graph.nodes.perception_node import _run_auto_compaction

    hist = [{"role": "assistant", "content": "a"}] * 10
    # rounds=5, last compacted at round=4 → gap=1 < 3
    state = _make_state(rounds=5, compaction_last_round=4)
    adapter = MagicMock()
    orc = MagicMock()

    new_hist, new_compacted = _run_auto_compaction(hist, adapter, orc, state)

    assert new_hist == hist
    assert new_compacted is None


def test_auto_compaction_skips_when_should_compact_false():
    """When CompactionService.should_compact() returns False, no compaction runs."""
    from src.core.orchestration.graph.nodes.perception_node import _run_auto_compaction

    hist = [{"role": "assistant", "content": "short"}]
    state = _make_state(rounds=10)
    adapter = MagicMock()
    orc = MagicMock()

    fake_service = MagicMock()
    fake_service.should_compact.return_value = False

    with patch(
        "src.core.memory.compaction_service.CompactionService",
        return_value=fake_service,
    ):
        new_hist, new_compacted = _run_auto_compaction(hist, adapter, orc, state)

    assert new_hist == hist
    assert new_compacted is None
    fake_service.compact.assert_not_called()


def test_auto_compaction_runs_and_emits_event():
    """When CompactionService.should_compact() returns True and compact() succeeds,
    the history is replaced and context.auto_compacted is published.
    """
    from src.core.orchestration.graph.nodes.perception_node import _run_auto_compaction

    from src.core.memory.compaction_service import CompactionResult

    hist = [
        {"role": "assistant", "content": "old1"},
        {"role": "assistant", "content": "old2"},
    ]
    compacted_hist = [{"role": "assistant", "content": "compact"}]
    state = _make_state(rounds=5)
    adapter = MagicMock()
    orc = MagicMock()
    events = []
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))

    fake_result = CompactionResult(
        success=True,
        compacted_history=compacted_hist,
        method="deterministic",
        tokens_before=100,
        tokens_after=25,
    )
    fake_service = MagicMock()
    fake_service.should_compact.return_value = True
    fake_service.compact.return_value = fake_result

    with patch(
        "src.core.memory.compaction_service.CompactionService",
        return_value=fake_service,
    ):
        new_hist, new_compacted = _run_auto_compaction(hist, adapter, orc, state)

    assert new_compacted is not None
    assert new_hist == compacted_hist
    assert any(e == "context.auto_compacted" for (e, _) in events)


def test_auto_compaction_no_op_on_compact_failure():
    """When compact() returns success=False, history is left unchanged."""
    from src.core.orchestration.graph.nodes.perception_node import _run_auto_compaction

    from src.core.memory.compaction_service import CompactionResult

    hist = [{"role": "user", "content": "something long " * 100}]
    state = _make_state(rounds=10)
    adapter = MagicMock()
    orc = MagicMock()

    fake_result = CompactionResult(
        success=False,
        method="error",
        error="LLM unavailable",
    )
    fake_service = MagicMock()
    fake_service.should_compact.return_value = True
    fake_service.compact.return_value = fake_result

    with patch(
        "src.core.memory.compaction_service.CompactionService",
        return_value=fake_service,
    ):
        new_hist, new_compacted = _run_auto_compaction(hist, adapter, orc, state)

    assert new_hist == hist
    assert new_compacted is None
