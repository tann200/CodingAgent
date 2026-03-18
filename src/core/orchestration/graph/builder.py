import logging
from typing import Literal

from langgraph.graph import StateGraph, END
from langchain_core.runnables import RunnableConfig
from src.core.orchestration.graph.state import AgentState
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

logger = logging.getLogger(__name__)


def should_after_planning(
    state: AgentState,
) -> Literal["execute", "memory_sync", "end"]:
    """
    Decide routing after the planning node.
    If a current_plan or next_action exists, execute. If last_result exists, memory_sync. Otherwise end.
    """
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


def should_after_plan_validator(
    state: AgentState,
) -> Literal["execute", "perception"]:
    """
    Decide routing after plan_validator node.
    If plan is valid, execute. If plan is invalid, go back to perception.
    """
    plan_validation = state.get("plan_validation")
    action_failed = state.get("action_failed")

    logger.info(
        f"should_after_plan_validator: validation={plan_validation}, action_failed={action_failed}"
    )

    if action_failed or not plan_validation or not plan_validation.get("valid", False):
        logger.info("should_after_plan_validator: plan invalid, going to perception")
        return "perception"

    logger.info("should_after_plan_validator: plan valid, executing")
    return "execute"


def route_after_perception(state: AgentState) -> Literal["execution", "analysis"]:
    """
    Phase 2.1: Fast-Path Routing
    If perception already generated a valid tool call (next_action), skip heavy
    analysis and planning and go directly to execution.

    This prevents over-engineering simple 1-step tasks that just need a quick tool call.
    """
    next_action = state.get("next_action")
    logger.info(f"route_after_perception: next_action={next_action is not None}")

    if next_action:
        logger.info("route_after_perception: Fast-path - going directly to execution")
        return "execution"

    logger.info("route_after_perception: Standard path - going to analysis")
    return "analysis"


def should_after_execution(
    state: AgentState,
) -> Literal["perception", "execution", "verification"]:
    """
    Decide routing after execution node.
    If there's no plan, go back to perception to generate next action.
    If there's a plan with more steps that haven't been completed, execute the next step directly.
    Otherwise go to verification.
    """
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    last_result = state.get("last_result")

    logger.info(
        f"should_after_execution: current_step={current_step}, plan_len={len(current_plan)}, last_result={last_result is not None}"
    )

    # Check if current step is completed
    if current_plan and current_step < len(current_plan):
        execution_ok = False
        if last_result:
            execution_ok = last_result.get("ok") or last_result.get("status") == "ok"

        if execution_ok:
            current_step_data = current_plan[current_step]
            current_step_data["completed"] = True
            next_step = current_step + 1

            if next_step < len(current_plan):
                # More steps - execute next step directly without going through perception
                logger.info(
                    f"should_after_execution: step completed, executing next step {next_step + 1}/{len(current_plan)}"
                )
                # Update state for next execution
                return "execution"
            else:
                # All steps complete, go to verification
                logger.info(
                    "should_after_execution: all steps complete, going to verification"
                )
                return "verification"
        else:
            # Step failed, go back to perception to try again
            logger.info("should_after_execution: step failed, going to perception")
            return "perception"

    # No plan - go back to perception to generate next action
    logger.info("should_after_execution: no plan, going to perception")
    return "perception"


def should_after_execution_with_replan(
    state: AgentState,
) -> Literal["perception", "execution", "verification", "replan"]:
    """
    Decide routing after execution node with replan support.
    If replan_required is set, route to replan_node.
    Otherwise use standard execution routing.
    """
    replan_required = state.get("replan_required")
    if replan_required:
        logger.info(
            f"should_after_execution_with_replan: replan required - {replan_required}"
        )
        return "replan"

    return should_after_execution(state)


def should_after_verification(
    state: AgentState,
) -> Literal["memory_sync", "debug", "end"]:
    """
    Decide routing after verification node.
    If verification passed, go to memory_sync.
    If verification failed and debug attempts remain, go to debug.
    Otherwise end.
    """
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


def should_after_replan(
    state: AgentState,
) -> Literal["step_controller", "perception"]:
    """
    Decide routing after replan node.
    After splitting oversized steps, go back to step_controller to execute the new smaller steps.
    """
    replan_required = state.get("replan_required")
    if replan_required:
        # Replan still has an issue, go to perception for help
        logger.info("should_after_replan: replan still required, going to perception")
        return "perception"

    # Replan successful, continue with step controller
    logger.info("should_after_replan: replan complete, going to step_controller")
    return "step_controller"


def should_after_evaluation(
    state: AgentState,
) -> Literal["memory_sync", "step_controller", "end"]:
    """
    Decide routing after evaluation node.
    If task is complete, go to memory_sync.
    If more work needed, go to step_controller.
    Otherwise end.
    """
    evaluation_result = state.get("evaluation_result", "complete")

    logger.info(f"should_after_evaluation: result={evaluation_result}")

    if evaluation_result == "complete":
        logger.info("should_after_evaluation: task complete, going to memory_sync")
        return "memory_sync"
    elif evaluation_result == "replan":
        logger.info(
            "should_after_evaluation: more work needed, going to step_controller"
        )
        return "step_controller"
    else:
        logger.info("should_after_evaluation: ending task")
        return "end"


def should_after_step_controller(
    state: AgentState,
) -> Literal["execution", "verification"]:
    """
    Step controller decides whether to proceed to execution or skip to verification.
    """
    import logging

    logger = logging.getLogger(__name__)

    current_plan = state.get("current_plan") or []
    current_step: int = int(state.get("current_step") or 0)
    last_result = state.get("last_result")

    logger.info(
        f"should_after_step_controller: current_step={current_step}, plan_len={len(current_plan)}, last_result={last_result}"
    )

    # If there's a plan with steps that haven't been reached yet, execute them
    # But only if the current step hasn't been attempted yet (current_step < len)
    if current_plan and current_step < len(current_plan):
        # Check if last result exists and was successful
        if last_result and isinstance(last_result, dict):
            if last_result.get("ok"):
                # Last execution was successful, check if we need to advance
                if current_step + 1 < len(current_plan):
                    # More steps after current, go to execution
                    logger.info(
                        f"should_after_step_controller: step {current_step + 1} done, more steps pending, going to execution"
                    )
                    return "execution"
                # Current is the last step, go to verification
                logger.info(
                    f"should_after_step_controller: step {current_step + 1} done, last step, going to verification"
                )
                return "verification"
            else:
                # Last execution failed, still at same step
                logger.info(
                    f"should_after_step_controller: step {current_step + 1} execution failed, going to verification"
                )
                return "verification"
        else:
            # No last_result yet (first time), go to execution
            logger.info(
                "should_after_step_controller: no last_result, going to execution"
            )
            return "execution"

    # No plan or at end of plan, go to verification
    logger.info("should_after_step_controller: no pending steps, going to verification")
    return "verification"

    # Only go to execution if there's an uncompleted pending step
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

    async def _replan(state: AgentState, config: RunnableConfig):
        return await replan_node(state, config)

    async def _evaluation(state: AgentState, config: RunnableConfig):
        return await evaluation_node(state, config)

    async def _plan_validator(state: AgentState, config: RunnableConfig):
        return await plan_validator_node(state, config)

    workflow.add_node("perception", _perception)
    workflow.add_node("analysis", _analysis)
    workflow.add_node("planning", _planning)
    workflow.add_node("plan_validator", _plan_validator)
    workflow.add_node("execution", _execution)
    workflow.add_node("step_controller", _step_controller)
    workflow.add_node("verification", _verification)
    workflow.add_node("debug", _debug)
    workflow.add_node("memory_sync", _memory_sync)
    workflow.add_node("replan", _replan)
    workflow.add_node("evaluation", _evaluation)

    # 2. Define Flow
    workflow.set_entry_point("perception")

    # Phase 2.1: Fast-Path Routing
    # If perception generated a tool call, skip analysis/planning and go directly to execution
    # Otherwise, go through the full cognitive pipeline
    workflow.add_conditional_edges(
        "perception",
        route_after_perception,
        {"execution": "execution", "analysis": "analysis"},
    )

    # analysis -> planning
    workflow.add_edge("analysis", "planning")

    # planning -> plan_validator (validate plan before execution)
    workflow.add_edge("planning", "plan_validator")

    # After plan_validator, execute or handle validation failure
    workflow.add_conditional_edges(
        "plan_validator",
        should_after_plan_validator,
        {"execute": "execution", "perception": "perception"},
    )

    # After planning decide if we execute, sync memory, or end
    # Note: This is now handled by plan_validator
    # workflow.add_conditional_edges(
    #     "planning",
    #     should_after_planning,
    #     {"execute": "execution", "memory_sync": "memory_sync", "end": END},
    # )

    # After execution, decide whether to continue with plan, replan, or go back to perception or verification
    workflow.add_conditional_edges(
        "execution",
        should_after_execution_with_replan,
        {
            "perception": "perception",
            "execution": "execution",
            "verification": "verification",
            "replan": "replan",
        },
    )

    # Replan -> step_controller (to execute new smaller steps)
    workflow.add_conditional_edges(
        "replan",
        should_after_replan,
        {"step_controller": "step_controller", "perception": "perception"},
    )

    # Step controller can still be used for enforcement but not in the main loop
    # Step controller -> execution or verification (only called from planning)
    workflow.add_conditional_edges(
        "step_controller",
        should_after_step_controller,
        {"execution": "execution", "verification": "verification"},
    )

    # After verification, go to evaluation for overall state review
    # Evaluation will check if task is complete or needs more work
    workflow.add_edge("verification", "evaluation")

    # Evaluation decides: memory_sync (complete), step_controller (more work), or end
    workflow.add_conditional_edges(
        "evaluation",
        should_after_evaluation,
        {
            "memory_sync": "memory_sync",
            "step_controller": "step_controller",
            "end": END,
        },
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
