from __future__ import annotations

from typing import Any, Literal, Mapping

from src.core.orchestration.graph.perception_routing import _task_is_complex


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
) -> Literal["analysis", "frontier_loop", "memory_sync"]:
    """Route after perception in the frontier graph.

    Complex tasks are routed through analysis + analyst_delegation first so
    LARGE/FRONTIER models receive the same pre-loop intelligence gathering as
    the standard capable graph.  Simple tasks and error conditions bypass
    analysis and go straight to frontier_loop or memory_sync.
    """
    if state.get("needs_clarification"):
        return "memory_sync"
    if "context_overflow" in (state.get("errors") or []):
        return "memory_sync"
    # next_action already set means fast-path (tool pre-planned); skip analysis.
    if state.get("next_action"):
        return "frontier_loop"
    if _task_is_complex(state):
        return "analysis"
    return "frontier_loop"


def route_frontier_loop_exit(
    state: Mapping[str, Any],
) -> Literal["verification", "memory_sync", "wait_for_user", "replan"]:
    if state.get("awaiting_plan_approval"):
        return "wait_for_user"
    if "context_overflow" in (state.get("errors") or []):
        return "memory_sync"
    # Phase 3.1: patch-size replan — a tool result flagged requires_split sets
    # replan_required; route to the replan node so the oversized step is split.
    if state.get("replan_required"):
        return "replan"
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


def route_replan_frontier(
    state: Mapping[str, Any],
    *,
    should_after_replan_fn: Any,
) -> Literal["frontier_loop", "memory_sync"]:
    """Route after the replan node in the frontier graph.

    ``should_after_replan`` returns ``step_controller`` (replan succeeded →
    resume work), ``perception`` (replan still needed → reframe), or
    ``memory_sync`` (recovery cap reached → bail).  The frontier graph has
    neither step_controller nor perception nodes, so both continuation
    targets re-enter the frontier loop; the cap-bail path still ends the
    pipeline the same way the full graph does.
    """
    result = should_after_replan_fn(state)
    if result == "memory_sync":
        return "memory_sync"
    return "frontier_loop"


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
    _ok_flag = last_result.get("ok")
    execution_ok = (_ok_flag is True) or (_ok_flag is None and last_result.get("status") == "ok")
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
) -> Literal["verification", "memory_sync"]:
    """Route after frontier_loop in the lite graph (P2.3).

    The lite graph previously routed unconditionally to ``memory_sync``, so its
    writes were never verified.  That made the P3-B policy inside
    ``verification_node`` ("SMALL tier: verify writes only") dead code on the
    lite path — the very path it was written for.

    Verification is now requested when, and only when, the loop's last tool was
    a write.  Read-only work skips the hop entirely, which keeps the lite tier's
    context/latency budget intact while still checking the edits that can
    actually break something.  ``last_tool_name`` is published by
    ``frontier_loop_node``; aliases are resolved so an alias cannot dodge the
    write check.
    """
    if state.get("needs_clarification"):
        return "memory_sync"
    if "context_overflow" in (state.get("errors") or []):
        return "memory_sync"

    # Minimum mutation verification on lite tier: route to verification if files
    # were modified, if a mutating action failed, or if the last tool was a write.
    modified = state.get("modified_files") or state.get("_session_modified_files") or []
    if modified:
        return "verification"

    last_res = state.get("last_result") or {}
    if isinstance(last_res, Mapping) and (last_res.get("ok") is False or last_res.get("status") in ("error", "failed")):
        return "verification"

    last_tool = str(state.get("last_tool_name") or "").strip()
    if not last_tool:
        return "memory_sync"

    try:
        from src.tools.constants import MODIFYING_TOOLS
        from src.tools.tools_config import resolve_tool_alias

        canonical = resolve_tool_alias(last_tool)
    except Exception:
        return "memory_sync"
    if canonical in MODIFYING_TOOLS or last_tool in MODIFYING_TOOLS:
        return "verification"
    return "memory_sync"


def route_verification_lite(
    state: Mapping[str, Any],
) -> Literal["frontier_loop", "memory_sync"]:
    """Route after verification in the lite graph.

    If verification passed, proceed to memory_sync.
    If verification failed, route back to frontier_loop up to max_debug_attempts
    (default 1 for lite); once attempts are exhausted, route to memory_sync.
    """
    if state.get("verification_passed") is False:
        debug_attempts = int(state.get("debug_attempts") or 0)
        max_debug_attempts = int(state.get("max_debug_attempts") or 1)
        if debug_attempts < max_debug_attempts:
            return "frontier_loop"
    return "memory_sync"



def _known_tiers() -> frozenset[str]:
    """Tiers accepted by ``select_tier_graph_cache_key``.

    Derived from the canonical ``ModelTier`` enum so adding a tier there cannot
    silently leave this router behind, plus the two legacy aliases the builder
    and CLI still pass.  ``nano`` is deliberately absent: it is not a
    ``ModelTier`` member, and ``_is_nano_or_small`` already treats it as
    non-small.
    """
    try:
        from src.core.inference.model_tiers import ModelTier

        names = {t.value for t in ModelTier}
    except Exception:
        names = {"small", "medium", "large", "frontier"}
    return frozenset(names | {"lite"})


def select_tier_graph_cache_key(tier: str) -> str:
    """Map a model tier to its compiled-graph cache key.

    Args:
        tier: Model tier string, case-insensitive.

    Returns:
        ``"lite"`` for the single-loop tier, otherwise ``"capable"``.

    Raises:
        ValueError: If *tier* is a non-empty string that is not a known tier.
            A typo such as ``"capabel"`` or ``"lites"`` used to be silently
            compiled as the capable graph, which also made the F-14 guard in
            ``compile_tier_graph_for_key`` unreachable.

    An empty/``None`` tier still falls back to ``"capable"``: it means tier
    classification failed, and degrading to the most capable graph is safer
    than crashing the run.
    """
    normalized_tier = (tier or "").lower()
    if not normalized_tier:
        return "capable"
    if normalized_tier in ("lite", "small"):
        return "lite"
    if normalized_tier in _known_tiers():
        return "capable"
    raise ValueError(
        f"select_tier_graph_cache_key: unknown model tier {tier!r}. "
        f"Known tiers: {sorted(_known_tiers())}. Add a mapping branch or fix "
        "the tier string."
    )



def compile_tier_graph_for_key(
    cache_key: str,
    *,
    compile_frontier_graph_fn: Any,
    compile_lite_graph_fn: Any,
) -> Any:
    if cache_key == "capable":
        return compile_frontier_graph_fn()
    if cache_key == "lite":
        return compile_lite_graph_fn()
    # F-14: unknown cache_key — fail loudly so a newly added tier is not
    # silently compiled as the frontier graph.
    raise ValueError(
        f"compile_tier_graph_for_key: unknown cache_key {cache_key!r}. "
        "Add a matching branch or update select_tier_graph_cache_key."
    )
