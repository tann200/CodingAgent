from __future__ import annotations

from typing import Any, Literal, Mapping


READ_ONLY_ROLES = {"scout", "researcher", "reviewer"}
WRITE_ROLES = {"coder", "tester"}


def should_use_prsw(state: Mapping[str, Any]) -> bool:
    delegations = list(state.get("delegations") or [])
    if len(delegations) < 2:
        return False

    has_read = any(d.get("role", "").lower() in READ_ONLY_ROLES for d in delegations)
    has_write = any(d.get("role", "").lower() in WRITE_ROLES for d in delegations)
    return has_read and has_write


def is_lite_mode(
    state: Mapping[str, Any],
    *,
    should_use_single_loop_fn: Any,
    is_nano_or_small_fn: Any,
) -> bool:
    if should_use_single_loop_fn is not None:
        model_name = state.get("model") or ""
        hardware_name = state.get("hardware_profile") or "auto"
        return should_use_single_loop_fn(model_name, hardware_name)
    return is_nano_or_small_fn(state)


def route_perception_frontier(
    state: Mapping[str, Any],
) -> Literal["frontier_loop", "memory_sync"]:
    if state.get("needs_clarification"):
        return "memory_sync"
    if "context_overflow" in (state.get("errors") or []):
        return "memory_sync"
    return "frontier_loop"


def route_frontier_loop_exit(
    state: Mapping[str, Any],
) -> Literal["verification", "memory_sync", "wait_for_user"]:
    if state.get("awaiting_plan_approval"):
        return "wait_for_user"
    if "context_overflow" in (state.get("errors") or []):
        return "memory_sync"
    last_result = state.get("last_result")
    if last_result is None:
        return "memory_sync"
    return "verification"


def route_wait_frontier(
    state: Mapping[str, Any],
) -> Literal["frontier_loop", "memory_sync"]:
    if state.get("plan_mode_approved"):
        return "frontier_loop"
    return "memory_sync"


def route_debug_frontier(
    state: Mapping[str, Any],
    *,
    should_after_debug_fn: Any,
) -> Literal["frontier_loop", "memory_sync", "end"]:
    result = should_after_debug_fn(state)
    if result == "execution":
        return "frontier_loop"
    return result


def should_after_memory_sync_frontier(
    state: Mapping[str, Any],
) -> Literal["delegation", "perception", "end"]:
    evaluation_result = state.get("evaluation_result") or ""
    if evaluation_result == "complete":
        return "end"
    if state.get("needs_clarification"):
        return "end"
    current_plan = state.get("current_plan") or []
    next_action = state.get("next_action")
    last_result = state.get("last_result") or {}
    execution_ok = last_result.get("ok") or last_result.get("status") == "ok"
    rounds = int(state.get("rounds") or 0)
    if not current_plan and not next_action and execution_ok and rounds > 0:
        return "end"
    delegations = state.get("delegations") or []
    if delegations:
        return "delegation"
    return "perception"


def route_perception_lite(
    state: Mapping[str, Any],
) -> Literal["frontier_loop", "memory_sync"]:
    if state.get("needs_clarification"):
        return "memory_sync"
    if "context_overflow" in (state.get("errors") or []):
        return "memory_sync"
    return "frontier_loop"


def route_frontier_loop_exit_lite(
    state: Mapping[str, Any],
) -> Literal["memory_sync"]:
    return "memory_sync"


def select_tier_graph_cache_key(tier: str) -> str:
    normalized_tier = (tier or "").lower()
    if normalized_tier in ("lite", "small"):
        return "lite"
    if normalized_tier in ("large", "frontier"):
        return "frontier"
    return "standard"


def compile_tier_graph_for_key(
    cache_key: str,
    *,
    compile_frontier_graph_fn: Any,
    compile_lite_graph_fn: Any,
    compile_agent_graph_fn: Any,
) -> Any:
    if cache_key == "frontier":
        return compile_frontier_graph_fn()
    if cache_key == "lite":
        return compile_lite_graph_fn()
    return compile_agent_graph_fn()
