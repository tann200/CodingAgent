"""test_routing_constants.py — single source of truth for routing thresholds.

Covers audit item 2.7 and WR-2: routing caps live in routing_constants.py and
the outer graph-round budget is reconciled there instead of being inlined as
magic numbers.
"""

from __future__ import annotations


def test_wr2_outer_graph_rounds_reconciled():
    from src.core.orchestration.graph import routing_constants as rc
    from src.core.orchestration import inference_loop as il
    from src.core.orchestration import inference_loop_rounds as ilr

    assert rc.MAX_GRAPH_ROUNDS == 20
    assert il.MAX_GRAPH_ROUNDS is rc.MAX_GRAPH_ROUNDS
    assert ilr.MAX_GRAPH_ROUNDS is rc.MAX_GRAPH_ROUNDS
    assert rc.MAX_ROUNDS_PLANNING == 15


def test_routing_caps_defined_once():
    from src.core.orchestration.graph import routing_constants as rc

    assert rc.DEFAULT_MAX_TOOL_CALLS == 30
    assert rc.LOOP_GUARD_ROUNDS == 10
    assert rc.RECOVERY_CAPS == {"small": 4, "medium": 8, "large": 12, "frontier": 12}
    assert rc.MAX_PLAN_ATTEMPTS == 3
    assert rc.MAX_STEP_RETRIES == 3
    assert rc.MAX_NO_PLAN_FAILS == 3
