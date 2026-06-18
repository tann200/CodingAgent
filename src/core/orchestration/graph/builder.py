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

# Fast-Path Stabilization: When True, all 16 cognitive nodes are active.
# When False (default during stabilization), only the Fast-Path nodes are active:
#   perception → execution → verification → evaluation → memory_sync
# Set to True to re-enable the full 16-node graph after stabilization is complete.
_USE_FULL_GRAPH = False


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


def _compile_full_graph():
    """Assemble the full 16-node LangGraph cognitive pipeline.

    All nodes active: perception, analysis, planning, plan_validator, execution,
    step_controller, verification, debug, evaluation, memory_sync, delegation,
    analyst_delegation, replan, wait_for_user.
    """
    workflow = StateGraph(AgentState)

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

    workflow.set_entry_point("perception")

    workflow.add_conditional_edges(
        "perception",
        route_after_perception,
        {
            "execution": "execution",
            "analysis": "analysis",
            "memory_sync": "memory_sync",
            "planning": "planning",
        },
    )

    workflow.add_conditional_edges(
        "analysis",
        should_after_analysis,
        {"analyst_delegation": "analyst_delegation", "planning": "planning"},
    )

    workflow.add_edge("analyst_delegation", "planning")
    workflow.add_edge("planning", "plan_validator")

    workflow.add_conditional_edges(
        "plan_validator",
        should_after_plan_validator,
        {
            "execute": "execution",
            "planning": "planning",
            "wait_for_user": "wait_for_user",
        },
    )

    workflow.add_conditional_edges(
        "execution",
        route_execution,
        {
            "wait_for_user": "wait_for_user",
            "step_controller": "step_controller",
            "perception": "perception",
            "memory_sync": "memory_sync",
            "replan": "replan",
            "analysis": "analysis",
        },
    )

    workflow.add_conditional_edges(
        "wait_for_user",
        route_after_wait_for_user,
        {
            "execute": "execution",
            "perception": "perception",
            "planning": "planning",
        },
    )

    workflow.add_conditional_edges(
        "replan",
        should_after_replan,
        {
            "step_controller": "step_controller",
            "perception": "perception",
            "memory_sync": "memory_sync",
        },
    )

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

    workflow.add_edge("verification", "evaluation")

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

    workflow.add_edge("delegation", END)

    return workflow.compile()


def _compile_fast_path_graph():
    """Assemble the Fast-Path graph (10 nodes, most cognitive nodes active).

    Stabilization Phase 5d: plan_validator validates plans before execution.

    Topology::

        perception → analysis → planning → plan_validator → execution → step_controller → verification → evaluation
            ↑                                                                              ↓              │
            └───────────────────────────────────── memory_sync ◄───────────────────────────┴──────────────┘
                                                                                   │
                                                                                   └──→ END

    Still frozen: replan, debug, delegation, analyst_delegation, wait_for_user.
    """
    workflow = StateGraph(AgentState)

    workflow.add_node("perception", perception_node)
    workflow.add_node("analysis", analysis_node)
    workflow.add_node("planning", planning_node)
    workflow.add_node("plan_validator", plan_validator_node)
    workflow.add_node("execution", execution_node)
    workflow.add_node("step_controller", step_controller_node)
    workflow.add_node("verification", verification_node)
    workflow.add_node("evaluation", evaluation_node)
    workflow.add_node("memory_sync", memory_update_node)

    workflow.set_entry_point("perception")

    # perception → analysis (complex tasks) | execution (fast-path) | planning
    # memory_sync when no action and last result OK
    workflow.add_conditional_edges(
        "perception",
        route_after_perception,
        {
            "execution": "execution",
            "analysis": "analysis",
            "memory_sync": "memory_sync",
            "planning": "planning",
        },
    )

    # analysis → planning (both simple and complex → planning)
    workflow.add_conditional_edges(
        "analysis",
        should_after_analysis,
        {
            "analyst_delegation": "planning",  # bypass: no analyst_delegation → planning
            "planning": "planning",
        },
    )

    # planning → plan_validator
    workflow.add_edge("planning", "plan_validator")

    # plan_validator → execution (plan approved) | planning (re-plan) | execution (bypass wait_for_user)
    workflow.add_conditional_edges(
        "plan_validator",
        should_after_plan_validator,
        {
            "execute": "execution",
            "planning": "planning",
            "wait_for_user": "execution",  # bypass: no wait_for_user in fast-path
        },
    )

    # execution → step_controller (plan step completed)
    #         → perception (continue loop)
    #         → memory_sync (task complete)
    workflow.add_conditional_edges(
        "execution",
        route_execution,
        {
            "wait_for_user": "execution",    # bypass: no user wait in fast-path
            "step_controller": "step_controller",
            "perception": "perception",
            "memory_sync": "memory_sync",
            "replan": "verification",         # bypass: replan→verify
            "analysis": "verification",       # bypass: fail→verify
        },
    )

    # step_controller → execution (next step) | verification (plan exhausted)
    #                 → planning (no plan)
    #                 → END (cancelled)
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

    # verification → evaluation
    workflow.add_edge("verification", "evaluation")

    # evaluation → step_controller (partial → next step) | memory_sync | end
    workflow.add_conditional_edges(
        "evaluation",
        should_after_evaluation,
        {
            "memory_sync": "memory_sync",
            "step_controller": "step_controller",
            "debug": "memory_sync",             # bypass: route to memory_sync
            "end": END,
        },
    )

    # memory_sync → perception (continue) | end
    workflow.add_conditional_edges(
        "memory_sync",
        should_after_memory_sync,
        {
            "delegation": END,      # bypass: no delegation in fast-path → end
            "perception": "perception",
            "end": END,
        },
    )

    return workflow.compile()


def compile_agent_graph():
    """
    Assembles the LangGraph cognitive pipeline.

    When ``_USE_FULL_GRAPH`` is True, all 16 cognitive nodes are active.
    When False (default, stabilization mode), only the 10 Fast-Path nodes are active:
    perception → analysis → planning → plan_validator → execution → step_controller → verification → evaluation → memory_sync.
    """
    if _USE_FULL_GRAPH:
        logger.info("compile_agent_graph: FULL graph (16 nodes)")
        return _compile_full_graph()
    logger.info("compile_agent_graph: FAST-PATH graph (10 nodes) — Phases 5a-5d")
    return _compile_fast_path_graph()


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
