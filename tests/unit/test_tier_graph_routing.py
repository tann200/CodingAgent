from src.core.orchestration.graph.tier_graph_routing import (
    compile_tier_graph_for_key,
    is_lite_mode,
    should_use_prsw,
    route_debug_frontier,
    route_frontier_loop_exit,
    route_frontier_loop_exit_lite,
    route_perception_frontier,
    route_perception_lite,
    route_wait_frontier,
    select_tier_graph_cache_key,
    should_after_memory_sync_frontier,
)
from unittest.mock import patch

from src.core.orchestration.graph.builder import get_compiled_graph_for_orchestrator


def test_route_perception_frontier_honors_clarification_and_overflow():
    assert route_perception_frontier({"needs_clarification": True}) == "memory_sync"
    assert route_perception_frontier({"errors": ["context_overflow"]}) == "memory_sync"
    assert route_perception_frontier({}) == "frontier_loop"


def test_route_frontier_loop_exit_handles_approval_overflow_and_empty_result():
    assert route_frontier_loop_exit({"awaiting_plan_approval": True}) == "wait_for_user"
    assert route_frontier_loop_exit({"errors": ["context_overflow"]}) == "memory_sync"
    assert route_frontier_loop_exit({"last_result": None}) == "memory_sync"
    assert route_frontier_loop_exit({"last_result": {"ok": True}}) == "verification"


def test_should_after_memory_sync_frontier_ends_on_completion_signal_without_plan():
    assert (
        should_after_memory_sync_frontier(
            {
                "current_plan": [],
                "next_action": None,
                "last_result": {"ok": True, "_completion_detected": True},
                "rounds": 1,
            }
        )
        == "end"
    )


def test_route_wait_frontier_requires_plan_approval():
    assert route_wait_frontier({"plan_mode_approved": True}) == "frontier_loop"
    assert route_wait_frontier({"plan_mode_approved": False}) == "memory_sync"


def test_route_debug_frontier_maps_execution_to_frontier_loop():
    assert (
        route_debug_frontier({}, should_after_debug_fn=lambda state: "execution")
        == "frontier_loop"
    )
    assert route_debug_frontier({}, should_after_debug_fn=lambda state: "end") == "end"


def test_should_after_memory_sync_frontier_handles_terminal_and_delegation_paths():
    assert should_after_memory_sync_frontier({"evaluation_result": "complete"}) == "end"
    assert should_after_memory_sync_frontier({"needs_clarification": True}) == "end"
    assert (
        should_after_memory_sync_frontier(
            {
                "current_plan": [],
                "next_action": None,
                "last_result": {"ok": True},
                "rounds": 1,
            }
        )
        == "end"
    )
    assert (
        should_after_memory_sync_frontier({"delegations": [{"role": "coder"}]})
        == "delegation"
    )
    assert should_after_memory_sync_frontier({}) == "perception"


def test_lite_routes_are_minimal():
    assert route_perception_lite({"needs_clarification": True}) == "memory_sync"
    assert route_perception_lite({"errors": ["context_overflow"]}) == "memory_sync"
    assert route_perception_lite({}) == "frontier_loop"
    assert route_frontier_loop_exit_lite({}) == "memory_sync"


def test_select_tier_graph_cache_key_maps_tiers_to_cache_keys():
    assert select_tier_graph_cache_key("frontier") == "capable"
    assert select_tier_graph_cache_key("large") == "capable"
    assert select_tier_graph_cache_key("lite") == "lite"
    assert select_tier_graph_cache_key("medium") == "capable"


def test_compile_tier_graph_for_key_dispatches_to_matching_compiler():
    calls = []

    result = compile_tier_graph_for_key(
        "capable",
        compile_frontier_graph_fn=lambda: calls.append("frontier") or "frontier-graph",
        compile_lite_graph_fn=lambda: calls.append("lite") or "lite-graph",
        compile_agent_graph_fn=lambda: calls.append("standard") or "standard-graph",
    )
    assert result == "frontier-graph"

    result = compile_tier_graph_for_key(
        "lite",
        compile_frontier_graph_fn=lambda: calls.append("frontier") or "frontier-graph",
        compile_lite_graph_fn=lambda: calls.append("lite") or "lite-graph",
        compile_agent_graph_fn=lambda: calls.append("standard") or "standard-graph",
    )
    assert result == "lite-graph"

    # F-14: "standard" and other unknown keys now raise ValueError rather than
    # silently falling through to frontier. See test_compile_tier_graph_for_key_raises_on_unknown_key.
    assert calls == ["frontier", "lite"]


def test_compile_tier_graph_for_key_raises_on_unknown_key():
    """F-14 fix: unknown cache_key must raise ValueError, not silently use frontier."""
    import pytest

    with pytest.raises(ValueError, match="unknown cache_key"):
        compile_tier_graph_for_key(
            "unknown_tier",
            compile_frontier_graph_fn=lambda: "frontier-graph",
            compile_lite_graph_fn=lambda: "lite-graph",
            compile_agent_graph_fn=lambda: "standard-graph",
        )


def test_get_compiled_graph_for_orchestrator_uses_explicit_model_tier():
    with patch(
        "src.core.orchestration.graph.builder.build_tier_graph",
        return_value="frontier-graph",
    ) as mock_build:
        result = get_compiled_graph_for_orchestrator(model="gpt-4o")

    assert result == "frontier-graph"
    mock_build.assert_called_once_with("frontier")


def test_should_use_prsw_requires_mixed_read_write_roles():
    assert (
        should_use_prsw({"delegations": [{"role": "scout"}, {"role": "coder"}]}) is True
    )
    assert (
        should_use_prsw({"delegations": [{"role": "scout"}, {"role": "reviewer"}]})
        is False
    )
    assert should_use_prsw({"delegations": [{"role": "coder"}]}) is False


def test_is_lite_mode_prefers_workflow_selector_when_available():
    calls = []

    result = is_lite_mode(
        {"model": "qwen3.5-9b", "hardware_profile": "rtx5070ti-16g"},
        should_use_single_loop_fn=lambda model, hardware: calls.append(
            (model, hardware)
        )
        or True,
        is_nano_or_small_fn=lambda state: False,
    )

    assert result is True
    assert calls == [("qwen3.5-9b", "rtx5070ti-16g")]


def test_is_lite_mode_falls_back_to_tier_check_without_workflow_selector():
    result = is_lite_mode(
        {"model_tier": "small"},
        should_use_single_loop_fn=None,
        is_nano_or_small_fn=lambda state: state.get("model_tier") == "small",
    )

    assert result is True
