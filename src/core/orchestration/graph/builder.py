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
from src.core.orchestration.graph.nodes.delegation_node import delegation_node
from src.core.orchestration.graph.nodes.analyst_delegation_node import analyst_delegation_node

logger = logging.getLogger(__name__)


def should_after_planning(
    state: AgentState,
) -> Literal["execute", "memory_sync", "end"]:
    """
    Routing after planning node.

    NOTE: This function is not wired as a conditional edge in compile_agent_graph().
    Routing from planning is handled by plan_validator_node instead.
    Kept for GraphFactory subgraphs and tests (W9 — low priority).
    """
    logger.info(
        f"should_after_planning: rounds={state.get('rounds')}, next_action={state.get('next_action')}, current_plan={state.get('current_plan')}"
    )
    if state.get("rounds", 0) >= 15:
        return "end"
    if state.get("next_action"):
        return "execute"
    current_plan = state.get("current_plan") or []
    if current_plan:
        return "execute"
    if state.get("last_result"):
        return "memory_sync"
    return "end"


def should_after_plan_validator(
    state: AgentState,
) -> Literal["execute", "perception"]:
    """
    Decide routing after plan_validator node.
    If plan is valid, execute. If plan is invalid, go back to perception.

    Loop guard: if rounds >= 8, force execution with whatever plan we have to avoid
    infinite plan_validator → perception → planning → plan_validator loops.
    Rounds only increments in perception_node, so this cap is reliable.
    """
    plan_validation = state.get("plan_validation")
    action_failed = state.get("action_failed")
    rounds = state.get("rounds", 0)

    logger.info(
        f"should_after_plan_validator: validation={plan_validation}, action_failed={action_failed}, rounds={rounds}"
    )

    # Loop guard: after 8 perception rounds, accept the plan regardless of warnings/errors
    # to prevent the plan_validator → perception cycle from running indefinitely.
    if rounds >= 8:
        logger.warning(
            f"should_after_plan_validator: rounds={rounds} >= 8, forcing execution to break potential loop"
        )
        return "execute"

    if action_failed or not plan_validation or not plan_validation.get("valid", False):
        logger.info("should_after_plan_validator: plan invalid, going to perception")
        return "perception"

    logger.info("should_after_plan_validator: plan valid, executing")
    return "execute"


_COMPLEXITY_KEYWORDS = (
    "refactor", "rewrite", "implement", "migrate", "redesign",
    "add feature", "add support", "create module", "create system",
    "integrate", "replace all", "convert all", "update all",
    "multi-step", "multiple files", "entire", "codebase",
)


def _task_is_complex(state: AgentState) -> bool:
    """
    W3: Heuristic to detect tasks that are too complex for the fast-path.

    Returns True when ANY of the following are true:
    - task description contains a complexity keyword
    - relevant_files list has more than 3 entries (analysis already ran and found scope)
    - current_plan is already set with 2+ steps (planning already ran)

    This prevents the fast-path from executing a single generated action when the
    true task requires coordinated analysis → planning → multi-step execution.
    """
    task: str = (state.get("task") or "").lower()
    if any(kw in task for kw in _COMPLEXITY_KEYWORDS):
        logger.info(f"route_after_perception: task classified as complex (keyword match)")
        return True

    relevant_files = state.get("relevant_files") or []
    if len(relevant_files) > 3:
        logger.info(
            f"route_after_perception: task classified as complex ({len(relevant_files)} relevant files)"
        )
        return True

    current_plan = state.get("current_plan") or []
    if len(current_plan) >= 2:
        logger.info(
            f"route_after_perception: task classified as complex ({len(current_plan)}-step plan already set)"
        )
        return True

    return False


def route_after_perception(state: AgentState) -> Literal["execution", "analysis"]:
    """
    Phase 2.1: Fast-Path Routing
    If perception already generated a valid tool call (next_action) AND the task is
    simple, skip heavy analysis and planning and go directly to execution.

    W3 fix: complex tasks (multi-step keywords, large file scope, or existing plan)
    are forced through analysis even when next_action is already set, to avoid
    executing a single hastily-generated action when coordinated planning is required.
    """
    next_action = state.get("next_action")
    logger.info(f"route_after_perception: next_action={next_action is not None}")

    if next_action:
        if _task_is_complex(state):
            logger.info(
                "route_after_perception: complex task detected — overriding fast-path, going to analysis"
            )
            return "analysis"
        logger.info("route_after_perception: simple task fast-path — going directly to execution")
        return "execution"

    logger.info("route_after_perception: Standard path - going to analysis")
    return "analysis"


def should_after_execution(
    state: AgentState,
) -> Literal["perception", "step_controller", "verification", "memory_sync"]:
    """
    Decide routing after execution node.
    If there's no plan and execution succeeded, task is complete - go to memory_sync.
    If there's a plan with more steps that haven't been completed, route to step_controller
    so it can load the next step description before execution (W5 fix).
    If execution failed, go back to perception to try again.
    Otherwise go to verification.

    W12: If tool_call_count >= max_tool_calls, bail to memory_sync to prevent runaway execution.
    """
    # W12: Enforce tool call budget
    tool_call_count = int(state.get("tool_call_count") or 0)
    max_tool_calls = int(state.get("max_tool_calls") or 30)
    if tool_call_count >= max_tool_calls:
        logger.warning(
            f"should_after_execution: tool budget exhausted "
            f"({tool_call_count}/{max_tool_calls}), routing to memory_sync"
        )
        return "memory_sync"

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
            # Don't mutate state in place — LangGraph state must be updated via returned dicts
            # The execution_node already returns an updated current_plan copy; this router
            # just uses the current state to decide routing without needing to mutate.
            next_step = current_step + 1

            if next_step < len(current_plan):
                # W5 fix: route through step_controller so it sets step_description
                # and planned_action for the next step before execution.
                logger.info(
                    f"should_after_execution: step completed, routing to step_controller for step {next_step + 1}/{len(current_plan)}"
                )
                return "step_controller"
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

    # No plan - check if execution succeeded
    if last_result:
        execution_ok = last_result.get("ok") or last_result.get("status") == "ok"
        if execution_ok:
            # Task completed successfully (single-step or final step)
            logger.info(
                "should_after_execution: no plan, task complete, going to memory_sync"
            )
            return "memory_sync"

    # No plan or execution not ok - go back to perception
    logger.info("should_after_execution: no plan, going to perception")
    return "perception"


def should_after_execution_with_replan(
    state: AgentState,
) -> Literal["perception", "step_controller", "verification", "replan", "memory_sync"]:
    """
    Decide routing after execution node with replan support.
    If replan_required is set, route to replan_node.
    Otherwise use standard execution routing.
    W12 budget check is delegated to should_after_execution.
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

    NOTE (NEW-15): This function is NOT wired in compile_agent_graph().
    The main graph uses a fixed edge: workflow.add_edge("verification", "evaluation").
    This function is used only in GraphFactory subgraphs.
    To make the main graph use this routing, replace the fixed edge with:
        workflow.add_conditional_edges("verification", should_after_verification, {...})
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
) -> Literal["memory_sync", "step_controller", "debug", "end"]:
    """
    Decide routing after evaluation node.
    - complete  → memory_sync (task done)
    - replan    → step_controller (remaining plan steps to execute)
    - debug     → debug (verification failed, generate a targeted fix)
    - anything else → end
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
    elif evaluation_result == "debug":
        logger.info("should_after_evaluation: verification failed, going to debug")
        return "debug"
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
                # execution_node already advanced current_step to next_step before
                # step_controller runs, so current_step IS the next unexecuted step.
                # Check current_step < len (not +1) to avoid the off-by-one (NEW-8).
                if current_step < len(current_plan):
                    # More steps remaining, go to execution
                    logger.info(
                        f"should_after_step_controller: advancing to step {current_step + 1}, more steps pending, going to execution"
                    )
                    return "execution"
                # All steps done, go to verification
                logger.info(
                    f"should_after_step_controller: all {len(current_plan)} steps done, going to verification"
                )
                return "verification"
            else:
                # F2: Last execution failed — retry, but cap retries per step (H2).
                MAX_STEP_RETRIES = 3
                step_retry_counts: dict = state.get("step_retry_counts") or {}
                retries = int(step_retry_counts.get(str(current_step), 0))
                if retries >= MAX_STEP_RETRIES:
                    logger.warning(
                        f"should_after_step_controller: step {current_step + 1} retry "
                        f"budget ({MAX_STEP_RETRIES}) exhausted, routing to verification "
                        f"(will trigger debug via evaluation)"
                    )
                    return "verification"
                logger.info(
                    f"should_after_step_controller: step {current_step + 1} execution "
                    f"failed (retry {retries}/{MAX_STEP_RETRIES}), going to execution"
                )
                return "execution"
        else:
            # No last_result yet (first time), go to execution
            logger.info(
                "should_after_step_controller: no last_result, going to execution"
            )
            return "execution"

    # No plan or at end of plan, go to verification
    logger.info("should_after_step_controller: no pending steps, going to verification")
    return "verification"


def should_after_analysis(
    state: AgentState,
) -> Literal["analyst_delegation", "planning"]:
    """
    #56: Route after analysis.

    Complex tasks (same heuristic as _task_is_complex) are sent through the
    analyst_delegation_node to get a deep-dive <findings> report before planning.
    Simple tasks go directly to planning to avoid the subagent overhead.
    """
    if _task_is_complex(state):
        logger.info(
            "should_after_analysis: complex task → analyst_delegation before planning"
        )
        return "analyst_delegation"
    logger.info("should_after_analysis: simple task → planning directly")
    return "planning"


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

    async def _delegation(state: AgentState, config: RunnableConfig):
        return await delegation_node(state, config)

    async def _analyst_delegation(state: AgentState, config: RunnableConfig):
        return await analyst_delegation_node(state, config)

    workflow.add_node("perception", _perception)
    workflow.add_node("analysis", _analysis)
    workflow.add_node("planning", _planning)
    workflow.add_node("plan_validator", _plan_validator)
    workflow.add_node("execution", _execution)
    workflow.add_node("step_controller", _step_controller)
    workflow.add_node("verification", _verification)
    workflow.add_node("debug", _debug)
    workflow.add_node("memory_sync", _memory_sync)
    workflow.add_node("delegation", _delegation)
    workflow.add_node("analyst_delegation", _analyst_delegation)
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

    # After execution, decide whether to continue via step_controller, replan,
    # go back to perception, or go to verification/memory_sync.
    # W5 fix: "execution" self-loop replaced with "step_controller" so the step
    # controller always loads the next step's description before execution.
    workflow.add_conditional_edges(
        "execution",
        should_after_execution_with_replan,
        {
            "perception": "perception",
            "step_controller": "step_controller",
            "verification": "verification",
            "replan": "replan",
            "memory_sync": "memory_sync",
        },
    )

    # Replan -> step_controller (to execute new smaller steps)
    workflow.add_conditional_edges(
        "replan",
        should_after_replan,
        {"step_controller": "step_controller", "perception": "perception"},
    )

    # Step controller -> execution or verification
    workflow.add_conditional_edges(
        "step_controller",
        should_after_step_controller,
        {"execution": "execution", "verification": "verification"},
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

    # After memory sync, check if there are delegations to spawn
    # Delegations run after memory_sync for background tasks
    workflow.add_edge("memory_sync", "delegation")

    # After delegation, always end — delegations are terminal (fire-and-forget after memory_sync).
    # Routing back to memory_sync caused an infinite loop because delegation_results is
    # always set (even as an empty dict) after the first delegation run.
    workflow.add_edge("delegation", END)

    return workflow.compile()
