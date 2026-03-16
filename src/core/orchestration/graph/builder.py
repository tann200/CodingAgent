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
    if state.get("current_plan"):
        # If current_plan has actionable steps
        if len(state.get("current_plan", [])) > 0:
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
) -> Literal["perception", "verification"]:
    """
    Decide routing after execution node.
    If there's a plan with more steps, loop back to perception. Otherwise go to verification.
    """
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0

    # If we have more steps in the plan, continue with perception
    if current_plan and current_step < len(current_plan) - 1:
        logger.info(
            f"should_after_execution: more steps ({current_step + 1}/{len(current_plan)}), looping to perception"
        )
        return "perception"

    # Otherwise proceed to verification
    logger.info("should_after_execution: no more steps, going to verification")
    return "verification"


def compile_agent_graph():
    """
    Assembles the LangGraph cognitive pipeline including Planning and Verification nodes.
    """
    import logging

    logger = logging.getLogger(__name__)

    workflow = StateGraph(AgentState)

    # 1. Add Nodes
    # Note: Explicitly define the nodes using function wrappers to avoid signature issues
    async def _perception(state: AgentState, config: RunnableConfig):
        return await perception_node(state, config)

    async def _planning(state: AgentState, config: RunnableConfig):
        return await planning_node(state, config)

    async def _execution(state: AgentState, config: RunnableConfig):
        return await execution_node(state, config)

    async def _verification(state: AgentState, config: RunnableConfig):
        return await verification_node(state, config)

    async def _memory_sync(state: AgentState, config: RunnableConfig):
        return await memory_update_node(state, config)

    workflow.add_node("perception", _perception)
    workflow.add_node("planning", _planning)
    workflow.add_node("execution", _execution)
    workflow.add_node("verification", _verification)
    workflow.add_node("memory_sync", _memory_sync)

    # 2. Define Flow
    workflow.set_entry_point("perception")

    # perception -> planning
    workflow.add_edge("perception", "planning")

    # After planning decide if we execute, sync memory, or end
    workflow.add_conditional_edges(
        "planning",
        should_after_planning,
        {"execute": "execution", "memory_sync": "memory_sync", "end": END},
    )

    # After execution, decide whether to continue with plan or go to verification
    workflow.add_conditional_edges(
        "execution",
        should_after_execution,
        {"perception": "perception", "verification": "verification"},
    )

    # After verification, always sync memory
    workflow.add_edge("verification", "memory_sync")

    # After memory sync, end
    workflow.add_edge("memory_sync", END)

    return workflow.compile()
