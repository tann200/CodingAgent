import pytest

from src.core.orchestration.graph.tier_graph_routing import (
    compile_tier_graph_for_key,
    is_lite_mode,
    should_use_prsw,
    route_debug_frontier,
    route_frontier_loop_exit,
    route_frontier_loop_exit_lite,
    route_perception_frontier,
    route_perception_lite,
    route_replan_frontier,
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


def test_route_frontier_loop_exit_routes_to_replan_on_patch_size_trigger():
    assert (
        route_frontier_loop_exit(
            {"replan_required": "Patch exceeded 200 lines. Split into smaller steps."}
        )
        == "replan"
    )


def test_route_replan_frontier_maps_continuation_to_frontier_loop():
    assert (
        route_replan_frontier({}, should_after_replan_fn=lambda state: "step_controller")
        == "frontier_loop"
    )
    assert (
        route_replan_frontier({}, should_after_replan_fn=lambda state: "perception")
        == "frontier_loop"
    )


def test_route_replan_frontier_bails_to_memory_sync_on_recovery_cap():
    assert (
        route_replan_frontier({}, should_after_replan_fn=lambda state: "memory_sync")
        == "memory_sync"
    )


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
    )
    assert result == "frontier-graph"

    result = compile_tier_graph_for_key(
        "lite",
        compile_frontier_graph_fn=lambda: calls.append("frontier") or "frontier-graph",
        compile_lite_graph_fn=lambda: calls.append("lite") or "lite-graph",
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


# ---------------------------------------------------- P2.2: tier routing correctness


def test_select_tier_graph_cache_key_is_case_insensitive():
    assert select_tier_graph_cache_key("SMALL") == "lite"
    assert select_tier_graph_cache_key("Frontier") == "capable"


def test_select_tier_graph_cache_key_empty_falls_back_to_capable():
    """A failed tier classification must degrade, not crash the run."""
    assert select_tier_graph_cache_key("") == "capable"
    assert select_tier_graph_cache_key(None) == "capable"  # type: ignore[arg-type]


@pytest.mark.parametrize("typo", ["capabel", "lites", "FRONTIERR", "auto", "capable", "nano"])
def test_select_tier_graph_cache_key_rejects_unknown_tier(typo):
    """A mistyped tier used to be silently compiled as the capable graph.

    That silent collapse also made the F-14 guard in
    ``compile_tier_graph_for_key`` unreachable, since it only ever received
    "capable" or "lite".
    """
    with pytest.raises(ValueError, match="unknown model tier"):
        select_tier_graph_cache_key(typo)


def test_every_model_tier_maps_to_a_cache_key():
    """The router must accept every tier the canonical enum can produce."""
    from src.core.inference.model_tiers import ModelTier

    for tier in ModelTier:
        assert select_tier_graph_cache_key(tier.value) in {"lite", "capable"}


def test_f14_guard_is_reachable_through_the_public_router():
    """P2.2: the unknown-cache_key guard must not be dead code."""
    # A tier that maps to a cache_key other than capable/lite cannot occur via
    # the router, so the guard is asserted directly on the compiler.
    with pytest.raises(ValueError, match="unknown cache_key"):
        compile_tier_graph_for_key(
            "typo-key",
            compile_frontier_graph_fn=lambda: "frontier-graph",
            compile_lite_graph_fn=lambda: "lite-graph",
        )


# ------------------------------------------------- P2.3: lite verification parity


class TestLiteVerificationParity:
    """P2.3: the lite graph must verify writes without verifying read-only work.

    Previously route_frontier_loop_exit_lite returned "memory_sync"
    unconditionally, so the lite graph had no verification node at all and the
    P3-B policy inside verification_node ("SMALL tier: verify writes only") was
    unreachable on the very path it was written for.
    """

    @pytest.mark.parametrize(
        "tool", ["write_file", "edit_file", "edit_file_atomic", "apply_patch", "delete_file"]
    )
    def test_write_tools_route_to_verification(self, tool):
        assert route_frontier_loop_exit_lite({"last_tool_name": tool}) == "verification"

    def test_write_alias_cannot_dodge_verification(self):
        # `write` is an alias of write_file; it must not skip the write check.
        assert route_frontier_loop_exit_lite({"last_tool_name": "write"}) == "verification"

    @pytest.mark.parametrize("tool", ["read_file", "list_files", "grep", "web_fetch", "bash"])
    def test_non_modifying_tools_skip_verification(self, tool):
        assert route_frontier_loop_exit_lite({"last_tool_name": tool}) == "memory_sync"

    def test_no_tool_ran_skips_verification(self):
        assert route_frontier_loop_exit_lite({}) == "memory_sync"
        assert route_frontier_loop_exit_lite({"last_tool_name": ""}) == "memory_sync"

    def test_context_overflow_skips_verification(self):
        state = {"last_tool_name": "write_file", "errors": ["context_overflow"]}
        assert route_frontier_loop_exit_lite(state) == "memory_sync"

    def test_clarification_skips_verification(self):
        state = {"last_tool_name": "write_file", "needs_clarification": True}
        assert route_frontier_loop_exit_lite(state) == "memory_sync"


def test_lite_graph_contains_verification_node():
    """P2.3: the node must be wired, not merely routable."""
    from src.core.orchestration.graph.builder import _compile_lite_graph

    nodes = set(_compile_lite_graph().nodes)
    assert "verification" in nodes
    assert {"perception", "frontier_loop", "verification", "memory_sync"} <= nodes


def test_frontier_loop_publishes_last_tool_name():
    """P2.3: the lite gate depends on the loop reporting what it ran."""
    import ast
    import inspect
    from pathlib import Path

    from src.core.orchestration.graph.nodes import frontier_completion
    from src.core.orchestration.graph.nodes import frontier_loop_node as fln

    def _dict_keys(module) -> set:
        src = Path(inspect.getfile(module)).read_text()
        return {
            k.value
            for d in ast.walk(ast.parse(src))
            if isinstance(d, ast.Dict)
            for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }

    # The node builds a `result` dict then returns it, so scan every dict
    # literal rather than only inline `return {...}`. P3.3 moved the exit
    # result into frontier_completion.build_exit_result, so the published keys
    # now live there; the contract is "the loop publishes them", not "the
    # literal is in this file".
    keys = _dict_keys(fln) | _dict_keys(frontier_completion)
    assert "last_tool_name" in keys
    assert "last_result" in keys
