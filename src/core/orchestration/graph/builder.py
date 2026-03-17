import logging
from typing import Literal

from langgraph.graph import StateGraph, END
from langchain_core.runnables import RunnableConfig
from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes.workflow_nodes import (
    perception_node,
    execution_node,
    memory_update_node,
    planning_node,
    verification_node,
    analysis_node,
    debug_node,
    step_controller_node,
)

logger = logging.getLogger(__name__)


def should_after_planning(
    state: AgentState,
) -> Literal["execute", "memory_sync", "end"]:
    """
    Decide routing after the planning node.
    If a current_plan or next_action exists, execute. If last_result exists, memory_sync. Otherwise end.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(
        f"should_after_planning: rounds={state.get('rounds')}, next_action={state.get('next_action')}, current_plan={state.get('current_plan')}, last_result={state.get('last_result')}"
    )

    # Safety cap
    if state["rounds"] >= 15:
        logger.info("should_after_planning: returning end (rounds >= 15)")
        return "end"
    if state.get("next_action"):
        logger.info("should_after_planning: returning execute (next_action exists)")
        return "execute"
    current_plan = state.get("current_plan")
    if current_plan:
        # If current_plan has actionable steps
        plan_len = len(current_plan) if current_plan else 0
        if plan_len > 0:
            logger.info(
                "should_after_planning: returning execute (current_plan has steps)"
            )
            return "execute"
    if state.get("last_result"):
        logger.info("should_after_planning: returning memory_sync (last_result exists)")
        return "memory_sync"
    logger.info("should_after_planning: returning end (no conditions met)")
    return "end"


def should_after_execution(
    state: AgentState,
) -> Literal["perception", "verification", "step_controller"]:
    """
    Decide routing after execution node.
    If there's a plan with more steps, loop back to perception.
    Otherwise go to step_controller for enforcement.
    """
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    step_controller_enabled = state.get("step_controller_enabled", True)

    # If we have more steps in the plan, continue with perception
    if current_plan and current_step < len(current_plan) - 1:
        logger.info(
            f"should_after_execution: more steps ({current_step + 1}/{len(current_plan)}), looping to perception"
        )
        return "perception"

    # If step controller is enabled, go there for enforcement
    if step_controller_enabled:
        logger.info("should_after_execution: going to step_controller")
        return "step_controller"

    # Otherwise proceed to verification
    logger.info("should_after_execution: no more steps, going to verification")
    return "verification"


def should_after_verification(
    state: AgentState,
) -> Literal["memory_sync", "debug", "end"]:
    """
    Decide routing after verification node.
    If verification passed, go to memory_sync.
    If verification failed and debug attempts remain, go to debug.
    Otherwise end.
    """
    import logging

    logger = logging.getLogger(__name__)

    verification_result = state.get("verification_result") or {}
    debug_attempts: int = int(state.get("debug_attempts") or 0)
    max_debug_attempts: int = int(state.get("max_debug_attempts") or 3)

    # Check if verification passed
    tests = verification_result.get("tests", {})
    linter = verification_result.get("linter", {})
    syntax = verification_result.get("syntax", {})

    passed = True
    if tests.get("status") == "fail":
        passed = False
    if linter.get("status") == "fail":
        passed = False
    if syntax.get("status") == "fail":
        passed = False

    if passed:
        logger.info(
            "should_after_verification: verification passed, going to memory_sync"
        )
        return "memory_sync"

    # Verification failed - check debug attempts
    if debug_attempts < max_debug_attempts:
        logger.info(
            f"should_after_verification: failed, going to debug (attempt {debug_attempts + 1}/{max_debug_attempts})"
        )
        return "debug"

    # Max debug attempts reached
    logger.warning(
        f"should_after_verification: max debug attempts reached ({max_debug_attempts}), ending"
    )
    return "end"


def should_after_debug(
    state: AgentState,
) -> Literal["execution", "memory_sync", "end"]:
    """
    Decide routing after debug node.
    If debug generated a fix action, go to execution.
    Otherwise go to memory_sync or end.
    """
    import logging

    logger = logging.getLogger(__name__)

    next_action = state.get("next_action")
    debug_attempts: int = int(state.get("debug_attempts") or 0)
    max_debug_attempts: int = int(state.get("max_debug_attempts") or 3)

    if next_action:
        logger.info("should_after_debug: fix generated, going to execution")
        return "execution"

    if debug_attempts >= max_debug_attempts:
        logger.info("should_after_debug: max attempts, going to memory_sync")
        return "memory_sync"

    logger.info("should_after_debug: no fix generated, ending")
    return "end"


def should_after_step_controller(
    state: AgentState,
) -> Literal["execution", "verification"]:
    """
    Step controller decides whether to proceed to execution or skip to verification.
    If there's a valid next action or plan step pending, go to execution.
    Otherwise go to verification.
    """
    import logging

    logger = logging.getLogger(__name__)

    current_plan = state.get("current_plan") or []
    current_step: int = int(state.get("current_step") or 0)

    if current_plan and current_step < len(current_plan):
        logger.info(
            f"should_after_step_controller: step {current_step + 1} pending, going to execution"
        )
        return "execution"

    logger.info("should_after_step_controller: no pending steps, going to verification")
    return "verification"


def compile_agent_graph():
    """
    Assembles the LangGraph cognitive pipeline with:
    - perception -> analysis -> planning -> execution -> step_controller -> verification
    - verification success -> memory_sync
    - verification failure -> debug -> execution (with retry limit)
    """
    workflow = StateGraph(AgentState)

    # 1. Add Nodes
    async def _perception(state: AgentState, config: RunnableConfig):
        return await perception_node(state, config)

    async def _analysis(state: AgentState, config: RunnableConfig):
        return await analysis_node(state, config)

    async def _planning(state: AgentState, config: RunnableConfig):
        return await planning_node(state, config)

    async def _execution(state: AgentState, config: RunnableConfig):
        return await execution_node(state, config)

    async def _step_controller(state: AgentState, config: RunnableConfig):
        return await step_controller_node(state, config)

    async def _verification(state: AgentState, config: RunnableConfig):
        return await verification_node(state, config)

    async def _debug(state: AgentState, config: RunnableConfig):
        return await debug_node(state, config)

    async def _memory_sync(state: AgentState, config: RunnableConfig):
        return await memory_update_node(state, config)

    workflow.add_node("perception", _perception)
    workflow.add_node("analysis", _analysis)
    workflow.add_node("planning", _planning)
    workflow.add_node("execution", _execution)
    workflow.add_node("step_controller", _step_controller)
    workflow.add_node("verification", _verification)
    workflow.add_node("debug", _debug)
    workflow.add_node("memory_sync", _memory_sync)

    # 2. Define Flow
    workflow.set_entry_point("perception")

    # perception -> analysis (NEW)
    workflow.add_edge("perception", "analysis")

    # analysis -> planning
    workflow.add_edge("analysis", "planning")

    # After planning decide if we execute, sync memory, or end
    workflow.add_conditional_edges(
        "planning",
        should_after_planning,
        {"execute": "execution", "memory_sync": "memory_sync", "end": END},
    )

    # After execution, decide whether to continue with plan or go to step_controller
    workflow.add_conditional_edges(
        "execution",
        should_after_execution,
        {
            "perception": "perception",
            "step_controller": "step_controller",
            "verification": "verification",
        },
    )

    # Step controller -> execution or verification
    workflow.add_conditional_edges(
        "step_controller",
        should_after_step_controller,
        {"execution": "execution", "verification": "verification"},
    )

    # After verification, branch based on result (NEW)
    workflow.add_conditional_edges(
        "verification",
        should_after_verification,
        {"memory_sync": "memory_sync", "debug": "debug", "end": END},
    )

    # Debug -> execution (retry) or memory_sync (NEW)
    workflow.add_conditional_edges(
        "debug",
        should_after_debug,
        {"execution": "execution", "memory_sync": "memory_sync", "end": END},
    )

    # After memory sync, end
    workflow.add_edge("memory_sync", END)

    return workflow.compile()
