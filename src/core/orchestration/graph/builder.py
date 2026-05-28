import logging
import threading
from typing import Any, Dict, Mapping

from langgraph.graph import StateGraph, END
from langchain_core.runnables import RunnableConfig
from src.core.orchestration.graph.state import AgentState, StateLike
from src.core.orchestration.graph.nodes.perception_node import perception_node
from src.core.orchestration.graph.nodes.analysis_node import analysis_node
from src.core.orchestration.graph.nodes.planning_node import planning_node
from src.core.orchestration.graph.nodes.plan_validator_node import plan_validator_node
from src.core.orchestration.graph.nodes.execution_node import execution_node
from src.core.orchestration.graph.nodes.step_controller_node import step_controller_node
from src.core.orchestration.graph.nodes.verification_node import verification_node
from src.core.orchestration.graph.nodes.debug_node import debug_node
from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node
from src.core.orchestration.graph.nodes.replan_node import replan_node
from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node
from src.core.orchestration.graph.nodes.delegation_node import delegation_node
from src.core.orchestration.graph.nodes.analyst_delegation_node import (
    analyst_delegation_node,
)
from src.core.orchestration.graph.nodes.wait_for_user_node import wait_for_user_node
from src.core.orchestration.graph import analysis_routing as _analysis_routing
from src.core.orchestration.graph import execution_routing as _execution_routing
from src.core.orchestration.graph import perception_routing as _perception_routing
from src.core.orchestration.graph import planning_routing as _planning_routing
from src.core.orchestration.graph import session_routing as _session_routing
from src.core.orchestration.graph import tier_graph_routing as _tier_graph_routing

logger = logging.getLogger(__name__)

# Backward-compatible re-exports for tests and node imports.
QUERY_TOOLS = _perception_routing.QUERY_TOOLS
READ_ONLY_TOOLS = _perception_routing.READ_ONLY_TOOLS
_COMPLEXITY_KEYWORDS = _perception_routing._COMPLEXITY_KEYWORDS
_is_large_or_frontier = _perception_routing._is_large_or_frontier
_is_nano_or_small = _perception_routing._is_nano_or_small
_task_has_more_steps = _perception_routing._task_has_more_steps
_task_is_complex = _perception_routing._task_is_complex
route_after_perception = _perception_routing.route_after_perception
should_after_analysis = _analysis_routing.should_after_analysis
should_after_plan_validator = _planning_routing.should_after_plan_validator
should_after_planning = _planning_routing.should_after_planning
should_after_step_controller = _planning_routing.should_after_step_controller
_DEFAULT_MAX_TOOL_CALLS = _execution_routing._DEFAULT_MAX_TOOL_CALLS
_LOOP_GUARD_ROUNDS = _execution_routing._LOOP_GUARD_ROUNDS
_RECOVERY_CAPS = _execution_routing._RECOVERY_CAPS
_check_no_plan_fast_path = _execution_routing._check_no_plan_fast_path
_check_plan_approval_pending = _execution_routing._check_plan_approval_pending
_check_preview_pending = _execution_routing._check_preview_pending
_check_replan_required = _execution_routing._check_replan_required
_check_tool_budget = _execution_routing._check_tool_budget
route_execution = _execution_routing.route_execution
should_after_debug = _execution_routing.should_after_debug
should_after_evaluation = _execution_routing.should_after_evaluation
should_after_execution = _execution_routing.should_after_execution
should_after_execution_with_compaction = (
    _execution_routing.should_after_execution_with_compaction
)
should_after_execution_with_replan = (
    _execution_routing.should_after_execution_with_replan
)
should_after_replan = _execution_routing.should_after_replan
should_after_verification = _execution_routing.should_after_verification
route_after_wait_for_user = _session_routing.route_after_wait_for_user
should_after_memory_sync = _session_routing.should_after_memory_sync

try:
    from src.core.orchestration.token_budget import (
        get_token_budget_monitor as _get_token_budget_monitor,
    )
except Exception:
    _get_token_budget_monitor = None  # type: ignore[assignment]

# v2: workflow_selector integration
WorkflowType: Any = None
try:
    from src.core.inference.workflow_selector import (
        WorkflowType,  # type: ignore[assignment]  # noqa: F401
        should_use_single_loop as _should_use_single_loop,
    )
except Exception:
    _should_use_single_loop = None  # type: ignore[assignment]

# D-11: Named routing constants — avoids magic numbers in router functions.
_MAX_ROUNDS_PLANNING = _planning_routing._MAX_ROUNDS_PLANNING
_AUTONOMOUS_MAX_TOOL_CALLS = 100  # default budget for autonomous/full graph


_READ_ONLY_ROLES = _tier_graph_routing.READ_ONLY_ROLES
_WRITE_ROLES = _tier_graph_routing.WRITE_ROLES


def should_use_prsw(state: Mapping[str, Any]) -> bool:
    """Compatibility wrapper for PRSW mixed-role delegation detection."""
    return _tier_graph_routing.should_use_prsw(state)


def _is_lite_mode(state: Mapping[str, Any]) -> bool:
    """Return True when v2 SINGLE_LOOP mode should be used.

    Uses workflow_selector if available (v2 Phase 0), otherwise falls back
    to tier check (NANO/SMALL without v2 integration).
    """
    return _tier_graph_routing.is_lite_mode(
        state,
        should_use_single_loop_fn=_should_use_single_loop,
        is_nano_or_small_fn=_is_nano_or_small,
    )


def compile_agent_graph():
    """
    Assembles the LangGraph cognitive pipeline with:
    - perception -> analysis -> planning -> execution -> step_controller -> verification
    - verification success -> memory_sync
    - verification failure -> debug -> execution (with retry limit)
    """
    workflow = StateGraph(AgentState)

    # 1. Add Nodes — pass node functions directly (no wrapper overhead)
    workflow.add_node("perception", perception_node)
    workflow.add_node("analysis", analysis_node)
    workflow.add_node("planning", planning_node)
    workflow.add_node("plan_validator", plan_validator_node)
    workflow.add_node("execution", execution_node)
    workflow.add_node("step_controller", step_controller_node)
    workflow.add_node("verification", verification_node)
    workflow.add_node("debug", debug_node)
    workflow.add_node("memory_sync", memory_update_node)
    workflow.add_node("delegation", delegation_node)
    workflow.add_node("analyst_delegation", analyst_delegation_node)
    workflow.add_node("replan", replan_node)
    workflow.add_node("evaluation", evaluation_node)
    workflow.add_node("wait_for_user", wait_for_user_node)

    # 2. Define Flow
    workflow.set_entry_point("perception")

    # Phase 2.1: Fast-Path Routing
    # - Tool call + simple -> execution
    # - No next action + last result OK -> memory_sync (task complete)
    # - Simple task (first round, no action) -> planning (bypass analysis)
    # - Otherwise -> analysis for context
    workflow.add_conditional_edges(
        "perception",
        route_after_perception,
        {
            "execution": "execution",
            "analysis": "analysis",
            "memory_sync": "memory_sync",
            "planning": "planning",  # P2-A: simple tasks bypass analysis
        },
    )

    # analysis -> analyst_delegation (complex) or planning (simple) — #56
    workflow.add_conditional_edges(
        "analysis",
        should_after_analysis,
        {"analyst_delegation": "analyst_delegation", "planning": "planning"},
    )

    # analyst_delegation -> planning (always — provides findings for planning prompt)
    workflow.add_edge("analyst_delegation", "planning")

    # planning -> plan_validator (validate plan before execution)
    workflow.add_edge("planning", "plan_validator")

    # After plan_validator, execute, re-plan, or wait for user approval (plan mode)
    workflow.add_conditional_edges(
        "plan_validator",
        should_after_plan_validator,
        {
            "execute": "execution",
            "planning": "planning",
            "wait_for_user": "wait_for_user",  # Plan Mode: valid plan needs user approval
        },
    )

    # After planning decide if we execute, sync memory, or end
    # Note: This is now handled by plan_validator
    # workflow.add_conditional_edges(
    #     "planning",
    #     should_after_planning,
    #     {"execute": "execution", "memory_sync": "memory_sync", "end": END},
    # )

    # After execution, decide whether to continue via step_controller, replan,
    # go back to perception, or go to verification/memory_sync.
    # W5 fix: "execution" self-loop replaced with "step_controller" so the step
    # controller always loads the next step's description before execution.
    workflow.add_conditional_edges(
        "execution",
        route_execution,
        {
            "wait_for_user": "wait_for_user",
            "step_controller": "step_controller",
            # WR-1 fix: fast-path routes
            "perception": "perception",
            "memory_sync": "memory_sync",
            # CF-2 fix: replan_required and W2 (fail→analysis) routes now live
            "replan": "replan",
            "analysis": "analysis",
        },
    )

    # wait_for_user -> execute (confirmed/approved), perception (preview rejected),
    #                   or planning (plan rejected — re-plan with feedback)
    workflow.add_conditional_edges(
        "wait_for_user",
        route_after_wait_for_user,
        {
            "execute": "execution",
            "perception": "perception",
            "planning": "planning",  # Plan Mode: user rejected plan → re-plan
        },
    )

    # Replan -> step_controller (to execute new smaller steps)
    workflow.add_conditional_edges(
        "replan",
        should_after_replan,
        {
            "step_controller": "step_controller",
            "perception": "perception",
            "memory_sync": "memory_sync",  # P2-A: global recovery cap exit
        },
    )

    # Step controller -> execution, verification, planning, or end
    # P3-T1: extended routing handles plan-exhausted (end), no-plan (planning),
    # and cancellation (end) in addition to the normal execution/verification paths.
    workflow.add_conditional_edges(
        "step_controller",
        should_after_step_controller,
        {
            "execution": "execution",
            "verification": "verification",
            "planning": "planning",
            "end": END,
        },
    )

    # After verification, go to evaluation for overall state review
    # Evaluation will check if task is complete or needs more work
    workflow.add_edge("verification", "evaluation")

    # Evaluation decides:
    #   complete     → memory_sync
    #   replan       → step_controller (remaining plan steps)
    #   debug        → debug (verification failed, generate fix — bounded by debug_attempts)
    #   end          → END
    workflow.add_conditional_edges(
        "evaluation",
        should_after_evaluation,
        {
            "memory_sync": "memory_sync",
            "step_controller": "step_controller",
            "debug": "debug",
            "end": END,
        },
    )

    # Debug → execution (apply fix) or memory_sync (give up) or end
    # debug_attempts is incremented by evaluation_node before routing here,
    # so this path is bounded to max_debug_attempts iterations.
    workflow.add_conditional_edges(
        "debug",
        should_after_debug,
        {"execution": "execution", "memory_sync": "memory_sync", "end": END},
    )

    workflow.add_conditional_edges(
        "memory_sync",
        should_after_memory_sync,
        {"delegation": "delegation", "perception": "perception", "end": END},
    )

    # After delegation, always end — delegations are terminal (fire-and-forget after memory_sync).
    # Routing back to memory_sync caused an infinite loop because delegation_results is
    # always set (even as an empty dict) after the first delegation run.
    workflow.add_edge("delegation", END)

    return workflow.compile()


# P1 fix: module-level singleton so the graph is compiled once per process.
# compile_agent_graph() does non-trivial work (validates edges, builds state machine);
# calling it on every run_agent_once() call added unnecessary startup latency.
# MED-13 fix: guard with a lock so concurrent threads don't each compile the graph
# and then race to store the result — only one compilation happens.
_COMPILED_GRAPH = None
_COMPILED_GRAPH_LOCK = threading.Lock()

# TASK-6: Per-tier graph cache.  Keys: "lite", "capable".
# Compiled once on first use; reset by _reset_compiled_graph().
_GRAPH_CACHE: Dict[str, Any] = {}
_GRAPH_CACHE_LOCK = threading.Lock()


def _get_compiled_graph():
    """Return the cached compiled agent graph, compiling it on first call."""
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        with _COMPILED_GRAPH_LOCK:
            if _COMPILED_GRAPH is None:
                _COMPILED_GRAPH = compile_agent_graph()
    return _COMPILED_GRAPH


def _reset_compiled_graph() -> None:
    """Reset the cached graph (for tests that need a fresh compile)."""
    global _COMPILED_GRAPH, _GRAPH_CACHE
    _COMPILED_GRAPH = None
    with _GRAPH_CACHE_LOCK:
        _GRAPH_CACHE.clear()


# ---------------------------------------------------------------------------
# TASK-6: Tier-aware graph factory
# ---------------------------------------------------------------------------


def _compile_frontier_graph():
    """Compile a lighter graph for LARGE/FRONTIER models.

    The frontier graph replaces the full 16-node pipeline with a
    ``frontier_loop_node`` that runs LLM+tool iterations internally,
    then routes to verification / memory_sync on exit.

    Graph topology (simplified)::

        perception → frontier_loop
        frontier_loop → verification | memory_sync | wait_for_user
        verification → evaluation → memory_sync | debug | end
        debug → execution | memory_sync | end
        memory_sync → delegation | perception | end
        delegation → end

    ``perception`` still runs so that tasks with ``next_action`` pre-set
    (fast-path) and context-overflow handling work correctly.
    """
    from src.core.orchestration.graph.nodes.frontier_loop_node import (
        frontier_loop_node,
    )

    workflow = StateGraph(AgentState)

    async def _perception(state: StateLike, config: RunnableConfig):
        return await perception_node(state, config)

    async def _analysis(state: StateLike, config: RunnableConfig):
        return await analysis_node(state, config)

    async def _analyst_delegation(state: StateLike, config: RunnableConfig):
        return await analyst_delegation_node(state, config)

    async def _frontier_loop(state: StateLike, config: RunnableConfig):
        return await frontier_loop_node(state, config)

    async def _verification(state: StateLike, config: RunnableConfig):
        return await verification_node(state, config)

    async def _evaluation(state: StateLike, config: RunnableConfig):
        return await evaluation_node(state, config)

    async def _debug(state: StateLike, config: RunnableConfig):
        return await debug_node(state, config)

    async def _memory_sync(state: StateLike, config: RunnableConfig):
        return await memory_update_node(state, config)

    async def _delegation(state: StateLike, config: RunnableConfig):
        return await delegation_node(state, config)

    async def _wait_for_user(state: StateLike, config: RunnableConfig):
        from src.core.orchestration.graph.nodes.wait_for_user_node import (
            wait_for_user_node,
        )

        return await wait_for_user_node(state, config)

    workflow.add_node("perception", _perception)
    workflow.add_node("analysis", _analysis)
    workflow.add_node("analyst_delegation", _analyst_delegation)
    workflow.add_node("frontier_loop", _frontier_loop)
    workflow.add_node("verification", _verification)
    workflow.add_node("evaluation", _evaluation)
    workflow.add_node("debug", _debug)
    workflow.add_node("memory_sync", _memory_sync)
    workflow.add_node("delegation", _delegation)
    workflow.add_node("wait_for_user", _wait_for_user)

    workflow.set_entry_point("perception")

    # perception → analysis (complex tasks) | frontier_loop | memory_sync
    # Complex tasks run analysis + analyst_delegation before entering the loop,
    # so LARGE/FRONTIER models benefit from the same pre-loop intelligence
    # gathering as the standard capable graph.
    workflow.add_conditional_edges(
        "perception",
        _tier_graph_routing.route_perception_frontier,
        {
            "analysis": "analysis",
            "frontier_loop": "frontier_loop",
            "memory_sync": "memory_sync",
        },
    )

    # analysis → analyst_delegation (complex) | frontier_loop (simple)
    # Reuse the standard routing function; remap "planning" key → "frontier_loop"
    # (same technique as step_controller → memory_sync remap above).
    workflow.add_conditional_edges(
        "analysis",
        should_after_analysis,
        {
            "analyst_delegation": "analyst_delegation",
            "planning": "frontier_loop",  # no planning node in frontier graph
        },
    )

    # analyst_delegation → frontier_loop (always — provides findings for loop context)
    workflow.add_edge("analyst_delegation", "frontier_loop")

    # frontier_loop → verification | memory_sync | wait_for_user
    workflow.add_conditional_edges(
        "frontier_loop",
        _tier_graph_routing.route_frontier_loop_exit,
        {
            "verification": "verification",
            "memory_sync": "memory_sync",
            "wait_for_user": "wait_for_user",
        },
    )

    # wait_for_user → frontier_loop (resume after plan approval)
    workflow.add_conditional_edges(
        "wait_for_user",
        _tier_graph_routing.route_wait_frontier,
        {"frontier_loop": "frontier_loop", "memory_sync": "memory_sync"},
    )

    # verification → evaluation
    workflow.add_edge("verification", "evaluation")

    # evaluation → memory_sync | debug | end
    workflow.add_conditional_edges(
        "evaluation",
        should_after_evaluation,
        {
            "memory_sync": "memory_sync",
            "step_controller": "memory_sync",  # no step_controller in frontier graph
            "debug": "debug",
            "end": END,
        },
    )

    # debug → frontier_loop (apply fix inline) | memory_sync | end
    workflow.add_conditional_edges(
        "debug",
        lambda state: _tier_graph_routing.route_debug_frontier(
            state,
            should_after_debug_fn=should_after_debug,
        ),
        {"frontier_loop": "frontier_loop", "memory_sync": "memory_sync", "end": END},
    )

    # memory_sync → delegation | perception | end
    workflow.add_conditional_edges(
        "memory_sync",
        _tier_graph_routing.should_after_memory_sync_frontier,
        {"delegation": "delegation", "perception": "perception", "end": END},
    )

    workflow.add_edge("delegation", END)

    return workflow.compile()


def _compile_lite_graph():
    """Compile a minimal graph for LITE mode (v2 Phase 1).

    The lite graph uses a single-loop node (frontier_loop_node) with
    reduced overhead:
    - No analysis node (context pressure)
    - No analyst_delegation (extra LLM call)
    - No verification node (tool result heuristics instead)
    - No replan node (retry + hint instead)

    Graph topology::

        perception → frontier_loop (lite config)
        frontier_loop → memory_sync | end
        memory_sync → end

    The lite config disables:
    - verification_node
    - evaluation_node
    - replan_node
    - analyst_delegation_node
    """
    from src.core.orchestration.graph.nodes.frontier_loop_node import (
        frontier_loop_node,
    )

    workflow = StateGraph(AgentState)

    async def _perception(state: StateLike, config: RunnableConfig):
        return await perception_node(state, config)

    async def _frontier_loop(state: StateLike, config: RunnableConfig):
        return await frontier_loop_node(state, config)

    async def _memory_sync(state: StateLike, config: RunnableConfig):
        return await memory_update_node(state, config)

    workflow.add_node("perception", _perception)
    workflow.add_node("frontier_loop", _frontier_loop)
    workflow.add_node("memory_sync", _memory_sync)

    workflow.set_entry_point("perception")

    workflow.add_conditional_edges(
        "perception",
        _tier_graph_routing.route_perception_lite,
        {"frontier_loop": "frontier_loop", "memory_sync": "memory_sync"},
    )

    workflow.add_conditional_edges(
        "frontier_loop",
        _tier_graph_routing.route_frontier_loop_exit_lite,
        {"memory_sync": "memory_sync"},
    )

    workflow.add_edge("memory_sync", END)

    return workflow.compile()


def _resolve_graph_tier(
    orchestrator: Any = None,
    *,
    model: str | None = None,
    adapter: Any = None,
) -> str:
    """Resolve the model tier used to select the compiled graph."""
    try:
        from src.core.inference.model_tiers import classify_model
        from src.core.inference.provider_utils import resolve_provider_capabilities

        resolved_adapter = adapter
        if resolved_adapter is None and orchestrator is not None:
            resolved_adapter = getattr(orchestrator, "_adapter", None) or getattr(
                orchestrator, "adapter", None
            )

        resolved_model = model
        if not resolved_model and orchestrator is not None:
            try:
                caps = resolve_provider_capabilities(orchestrator, resolved_adapter)
            except Exception:
                caps = {}
            resolved_model = caps.get("model") or None

        if not resolved_model and resolved_adapter is not None:
            try:
                resolved_model = getattr(resolved_adapter, "default_model", None)
            except Exception:
                resolved_model = None
            if not resolved_model:
                try:
                    models_attr = getattr(resolved_adapter, "models", None)
                    if isinstance(models_attr, (list, tuple)):
                        for candidate in models_attr:
                            if candidate:
                                resolved_model = str(candidate)
                                break
                    elif models_attr:
                        resolved_model = str(models_attr)
                except Exception:
                    resolved_model = None

        if not resolved_model:
            return "medium"

        context_window = 0
        try:
            if resolved_adapter is not None and hasattr(
                resolved_adapter, "context_window"
            ):
                context_window = int(resolved_adapter.context_window or 0)
        except Exception:
            context_window = 0

        return classify_model(str(resolved_model), context_window).value
    except Exception:
        return "medium"


def get_compiled_graph_for_orchestrator(
    orchestrator: Any = None,
    *,
    model: str | None = None,
    adapter: Any = None,
):
    """Return the compiled graph selected for the current orchestrator/model tier."""
    tier = _resolve_graph_tier(orchestrator, model=model, adapter=adapter)
    return build_tier_graph(tier)


def build_tier_graph(tier: str):
    """Return a compiled LangGraph graph appropriate for *tier*.

    Parameters
    ----------
    tier:
        Model tier string (e.g. ``"frontier"``, ``"large"``, ``"medium"``,
        ``"lite"``). Case-insensitive.
        - ``"lite"`` gets the single-loop graph
        - all other tiers use the capable loop graph

    Returns
    -------
    CompiledStateGraph
        Thread-safe, cached compiled graph.

    Notes
    -----
    - Compiled once per tier key, then stored in ``_GRAPH_CACHE``.
    - Thread-safe: uses ``_GRAPH_CACHE_LOCK``.
    - Callers can force recompilation by calling ``_reset_compiled_graph()``.
    """
    cache_key = _tier_graph_routing.select_tier_graph_cache_key(tier)

    # Fast path (no lock)
    cached = _GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    with _GRAPH_CACHE_LOCK:
        # Re-check under lock to avoid double compilation race
        cached = _GRAPH_CACHE.get(cache_key)
        if cached is not None:
            return cached
        logger.info("build_tier_graph: compiling '%s' graph", cache_key)
        compiled = _tier_graph_routing.compile_tier_graph_for_key(
            cache_key,
            compile_frontier_graph_fn=_compile_frontier_graph,
            compile_lite_graph_fn=_compile_lite_graph,
        )
        _GRAPH_CACHE[cache_key] = compiled
        logger.info("build_tier_graph: '%s' graph compiled and cached", cache_key)
        return compiled
