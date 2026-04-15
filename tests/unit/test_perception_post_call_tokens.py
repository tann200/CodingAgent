from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _make_state(history=None, session_id: str = "s1", rounds: int = 0):
    return {"history": history or [], "session_id": session_id, "rounds": rounds}


def test_reactive_overflow_early_exit_emits_event_and_truncates_history():
    from src.core.orchestration.graph.nodes.perception_node import (
        _process_post_call_tokens,
    )

    events: list[tuple[str, Any]] = []
    orc = MagicMock()
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))

    state = _make_state(history=[{"role": "assistant", "content": "x"}] * 10)
    resp = {"context_overflow": True}

    early, overflow_compaction, session_cost = _process_post_call_tokens(
        resp, state, orc, adapter=None
    )

    assert early is not None, "Expected early exit dict on reactive overflow"
    assert "_compacted_history" in early, "Early-exit must include _compacted_history"
    assert "context_overflow" in early.get("errors", []), (
        "errors must include context_overflow"
    )
    assert overflow_compaction.get("_budget_compaction") is True
    assert overflow_compaction.get("_should_distill") is True
    assert session_cost == 0.0
    assert any(e == "context.overflow" for (e, _) in events)


def test_postcall_overflow_records_usage_and_estimates_cost():
    from src.core.orchestration.graph.nodes import perception_node as pn
    from src.core.orchestration.graph.nodes.perception_node import (
        _process_post_call_tokens,
    )

    events: list[tuple[str, Any]] = []
    orc = MagicMock()
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))
    orc.token_monitor = MagicMock()
    orc.token_monitor.record_usage = MagicMock()

    adapter = MagicMock()
    adapter.default_model = "m1"

    # prompt_tokens above available threshold when budget=8000 and reserve=4096
    prompt_tokens = 4004
    resp = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 50,
        "total_tokens": prompt_tokens + 50,
        "model": "m1",
    }

    state = _make_state()

    # Patch get_actual_context_window to 8000 and estimate_cost_usd to a predictable value
    with (
        patch(
            "src.core.inference.provider_context.get_actual_context_window",
            return_value=8000,
        ),
        patch.object(pn, "_estimate_cost_usd", return_value=0.42),
    ):
        early, overflow_compaction, session_cost = _process_post_call_tokens(
            resp, state, orc, adapter
        )

    assert early is None
    assert overflow_compaction.get("_budget_compaction") is True
    assert overflow_compaction.get("_should_distill") is True
    # token monitor should have been invoked
    orc.token_monitor.record_usage.assert_called_once()
    # event published
    assert any(e == "context.overflow" for (e, _) in events)
    # cost delta should be the patched value
    assert session_cost == 0.42


def test_no_usage_no_overflow_returns_empty():
    from src.core.orchestration.graph.nodes.perception_node import (
        _process_post_call_tokens,
    )

    orc = MagicMock()
    orc.event_bus = MagicMock()
    orc.token_monitor = None

    resp = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    state = _make_state()

    early, overflow_compaction, session_cost = _process_post_call_tokens(
        resp, state, orc, adapter=None
    )

    assert early is None
    assert overflow_compaction == {}
    assert session_cost == 0.0


def test_missing_estimator_and_token_monitor_is_tolerated():
    """When token_monitor is absent and cost estimator is missing or raises,
    the helper should not raise and should return sane defaults."""
    from src.core.orchestration.graph.nodes.perception_node import (
        _process_post_call_tokens,
    )

    # orchestrator without token_monitor
    orc = MagicMock()
    orc.event_bus = MagicMock()
    orc.token_monitor = None

    # simulate publish raising to ensure helper tolerates it
    def _pub(e, p):
        raise RuntimeError("bus fail")

    orc.event_bus.publish = _pub

    # resp with usage but estimate function missing/raises
    resp = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    state = _make_state()

    # Patch provider_context.get_actual_context_window to raise to hit fallback
    with patch(
        "src.core.inference.provider_context.get_actual_context_window",
        side_effect=Exception("no ctx"),
    ):
        early, overflow_compaction, session_cost = _process_post_call_tokens(
            resp, state, orc, adapter=None
        )

    assert early is None
    # no token monitor -> no recording; overflow_compaction may be empty
    assert isinstance(overflow_compaction, dict)
    assert session_cost == 0.0


def test_event_bus_publish_absent_and_estimator_raises_does_not_crash():
    """If event_bus.publish is missing (or not callable) and estimator raises,
    the helper should handle gracefully."""
    from src.core.orchestration.graph.nodes import perception_node as pn
    from src.core.orchestration.graph.nodes.perception_node import (
        _process_post_call_tokens,
    )

    orc = MagicMock()
    # event_bus without publish attribute
    orc.event_bus = object()
    orc.token_monitor = MagicMock()
    orc.token_monitor.record_usage = MagicMock()

    adapter = MagicMock()
    adapter.default_model = "model-x"

    resp = {
        "prompt_tokens": 10000,
        "completion_tokens": 10,
        "total_tokens": 10010,
        "model": "model-x",
    }
    state = _make_state()

    # Patch get_actual_context_window to a large value so no overflow path is hit,
    # and patch _estimate_cost_usd to raise when called.
    with (
        patch(
            "src.core.inference.provider_context.get_actual_context_window",
            return_value=80000,
        ),
        patch.object(pn, "_estimate_cost_usd", side_effect=RuntimeError("bad price")),
    ):
        early, overflow_compaction, session_cost = _process_post_call_tokens(
            resp, state, orc, adapter
        )

    assert early is None
    # token monitor should have been called
    orc.token_monitor.record_usage.assert_called()
    # estimator raised -> session_cost should remain 0.0 (handled)
    assert session_cost == 0.0
