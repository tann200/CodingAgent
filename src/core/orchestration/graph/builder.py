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
from src.core.orchestration.graph.nodes.analyst_delegation_node import (
    analyst_delegation_node,
)

logger = logging.getLogger(__name__)


def should_after_planning(
    state: AgentState,
) -> Literal["execute", "memory_sync", "end"]:
    """
    Routing after planning node.

    NOT WIRED IN compile_agent_graph() — the main graph routes planning →
    plan_validator_node unconditionally. This function is kept for GraphFactory
    subgraphs that bypass plan_validator. Do not call from main graph code.
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
) -> Literal["execute", "planning", "wait_for_user"]:
    """
    Decide routing after plan_validator node.

    Valid plan + plan_mode_enabled (and not yet approved) → wait_for_user for approval.
    Valid plan (no plan mode, or already approved)        → execute directly.
    Invalid plan                                           → planning (F10 fix).
    Emergency loop guard (rounds >= 8)                    → execute to break cycle.
    Resumed plan (plan_resumed=True)                      → execute directly (CF-5 fix).
    """
    plan_validation = state.get("plan_validation")
    action_failed = state.get("action_failed")
    rounds = state.get("rounds", 0)
    plan_attempts = state.get("plan_attempts", 0)

    logger.info(
        f"should_after_plan_validator: validation={plan_validation}, action_failed={action_failed}, rounds={rounds}, plan_attempts={plan_attempts}"
    )

    # CF-5 fix: A resumed plan was already validated in a prior session — skip
    # re-validation and go straight to execution at the saved step.
    if state.get("plan_resumed", False):
        logger.info(
            "should_after_plan_validator: plan_resumed=True — skipping re-validation, executing"
        )
        return "execute"

    # Emergency loop guard: after 8 rounds (perception cycles), force execution.
    if rounds >= 8:
        logger.warning(
            f"should_after_plan_validator: rounds={rounds} >= 8, forcing execution to break loop"
        )
        return "execute"

    # P1-2: Inner planning loop guard — break infinite planning→validator→planning cycle.
    if plan_attempts >= 3:
        logger.warning(
            f"should_after_plan_validator: plan_attempts={plan_attempts} >= 3, forcing execution"
        )
        return "execute"

    if action_failed or not plan_validation or not plan_validation.get("valid", False):
        # F10 fix: route directly to planning (not perception) to save 2 LLM calls.
        # planning_node already has repo context from analysis; no need for re-perception.
        logger.info("should_after_plan_validator: plan invalid, re-planning (F10)")
        return "planning"

    # Plan is valid — check if plan mode requires user approval before execution.
    if state.get("plan_mode_enabled", False) and not state.get(
        "plan_mode_approved", False
    ):
        logger.info(
            "should_after_plan_validator: plan_mode enabled, suspending for user approval"
        )
        return "wait_for_user"

    logger.info("should_after_plan_validator: plan valid, executing")
    return "execute"


_READ_ONLY_ROLES = {"scout", "researcher", "reviewer"}
_WRITE_ROLES = {"coder", "tester"}


def should_use_prsw(state: AgentState) -> bool:
    """
    Determine if PRSW execution should be used.

    Returns True if:
    - Multiple delegations exist with mixed read/write roles
    - Or execution_waves has multiple waves with different step types
    """
    delegations = state.get("delegations", [])
    if len(delegations) < 2:
        return False

    has_read = any(d.get("role", "").lower() in _READ_ONLY_ROLES for d in delegations)
    has_write = any(d.get("role", "").lower() in _WRITE_ROLES for d in delegations)

    return has_read and has_write


import re as _re

# HR-7 fix: exact multi-word phrases are safe to match as substrings; single
# ambiguous words ("add", "edit", etc.) are matched with word-boundary regex
# so that incidental occurrences ("authentication", "before you know it") do not
# false-positive trigger expensive analyst_delegation.
_COMPLEXITY_KEYWORDS_EXACT = (
    "refactor",
    "rewrite",
    "implement",
    "migrate",
    "redesign",
    "add feature",
    "add support",
    "create module",
    "create system",
    "integrate",
    "replace all",
    "convert all",
    "update all",
    "multi-step",
    "multiple files",
    "entire",
    "codebase",
)

# These short verbs are matched with word boundaries to avoid false positives.
# HR-2 fix: Only genuinely multi-step verbs remain. Common single-file action
# verbs (add, edit, change, update, delete, remove, insert, modify, append,
# prepend) were removed because they fire on virtually all coding tasks,
# making the fast-path dead code and forcing 6 extra LLM calls per simple edit.
_COMPLEXITY_KEYWORDS_WORD = (
    "refactor",
    "rewrite",
    "implement",
    "migrate",
    "migrate all",
    "restructure",
)

_COMPLEXITY_WORD_RE = _re.compile(
    r"\b(?:" + "|".join(_COMPLEXITY_KEYWORDS_WORD) + r")\b"
)

# Keep the old name for backwards compat (tests may reference it)
_COMPLEXITY_KEYWORDS = _COMPLEXITY_KEYWORDS_EXACT + tuple(
    kw + " " for kw in _COMPLEXITY_KEYWORDS_WORD
)


def _task_is_complex(state: AgentState) -> bool:
    """
    W3: Heuristic to detect tasks that are too complex for the fast-path.

    Returns True when ANY of the following are true:
    - task description contains a complexity keyword (exact phrase or word match)
    - relevant_files list has more than 3 entries (analysis already ran and found scope)
    - current_plan is already set with 2+ steps (planning already ran)

    HR-7 fix: short ambiguous keywords like "add", "edit" are now matched with
    word-boundary regex so strings like "authentication" or "before you know it"
    no longer false-positive trigger analyst_delegation.
    """
    task: str = (state.get("task") or "").lower()
    if any(kw in task for kw in _COMPLEXITY_KEYWORDS_EXACT):
        logger.info(
            "route_after_perception: task classified as complex (exact keyword match)"
        )
        return True
    if _COMPLEXITY_WORD_RE.search(task):
        logger.info(
            "route_after_perception: task classified as complex (word-boundary keyword match)"
        )
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


def _task_has_more_steps(state: AgentState) -> bool:
    """
    Detect if the task likely has more steps needed.

    Checks for multi-step indicators in the original task description:
    - Sequential keywords: "and", "then", "after that", "next", "also", "as well"
    - Multiple file operations with "and" connector
    - Common multi-step patterns

    Returns True if the task appears to have more steps beyond what was just executed.
    """
    original_task = state.get("original_task") or ""
    task = state.get("task") or ""
    combined_task = f"{original_task} {task}".lower()

    multi_step_patterns = [
        r"\band\b",  # "create folder and file"
        r"\bthen\b",  # "do this then do that"
        r"\bafter that\b",
        r"\bnext\b",
        r"\balso\b",
        r"\bas well\b",
        r",\s*\w+\s+and\s+\w+",  # "file1, file2 and file3"
    ]

    for pattern in multi_step_patterns:
        import re

        if re.search(pattern, combined_task):
            tool_call_count = int(state.get("tool_call_count") or 0)
            if tool_call_count < 3:
                logger.info(
                    f"route_after_perception: multi-step task detected "
                    f"(pattern='{pattern}', tool_calls={tool_call_count})"
                )
                return True

    return False


def route_after_perception(
    state: AgentState,
) -> Literal["execution", "analysis", "memory_sync"]:
    """
    Phase 2.1: Fast-Path Routing.
    If perception generated a valid tool call and task is simple, go to execution.
    Complex tasks are forced through analysis.

    CRITICAL FIX: When next_action=None after successful execution:
    - If task appears to have more steps (multi-step detected), continue to analysis
    - Otherwise, route to memory_sync for final distillation.

    This ensures multi-step tasks like "create folder and file" continue executing
    instead of prematurely ending after the first tool call.
    """
    next_action = state.get("next_action")
    last_result = state.get("last_result")
    rounds = state.get("rounds", 0)

    logger.info(
        f"route_after_perception: next_action={next_action is not None}, "
        f"rounds={rounds}"
    )

    if next_action:
        if _task_is_complex(state):
            logger.info(
                "route_after_perception: complex task - overriding fast-path, "
                "going to analysis"
            )
            return "analysis"
        logger.info(
            "route_after_perception: simple task fast-path - going to execution"
        )
        return "execution"

    if last_result and rounds > 0:
        execution_ok = last_result.get("ok") or last_result.get("status") == "ok"
        if execution_ok:
            if _task_has_more_steps(state):
                logger.info(
                    "route_after_perception: task has more steps, continuing execution"
                )
                return "analysis"
            logger.info("route_after_perception: task complete, going to memory_sync")
            return "memory_sync"

    logger.info("route_after_perception: no action yet, going to analysis")
    return "analysis"


def should_after_execution(
    state: AgentState,
) -> Literal[
    "perception", "analysis", "step_controller", "verification", "memory_sync"
]:
    """
    Decide routing after execution node.
    - No plan + execution succeeded -> go to perception (task likely not done)
    - Plan with more steps -> step_controller
    - Plan step failed -> perception (retry)
    - Fast-path failed (no plan) -> analysis (deeper context)
    - Otherwise -> verification
    W12: If tool_call_count >= max_tool_calls, bail to memory_sync.
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

    # Phase A: Check for wave-based execution completion
    execution_waves = state.get("execution_waves")
    current_wave = state.get("current_wave") or 0
    if execution_waves and current_wave < len(execution_waves):
        wave_step_ids = execution_waves[current_wave]
        logger.info(
            f"should_after_execution: wave {current_wave + 1}/{len(execution_waves)}, "
            f"steps={wave_step_ids}"
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
            # After a read tool, check if task implies modification that needs another tool call
            task = (state.get("task") or "").lower()
            last_tool = state.get("last_tool_name")
            modification_keywords = (
                "add ",
                "prepend",
                "append",
                "edit ",
                "modify",
                "update ",
                "change ",
                "replace ",
                "insert ",
                "delete ",
                "remove ",
                "top of ",
                "beginning of ",
                "after ",
                "before ",
                "on top of ",
                "inside ",
                "contents of ",
            )
            read_then_modify = last_tool in ("read_file", "fs.read") and any(
                kw in task for kw in modification_keywords
            )
            if read_then_modify:
                logger.info(
                    "should_after_execution: read done, task implies mod, "
                    "going back to perception"
                )
                return "perception"

            # After execution, go to perception to decide next action.
            # Do NOT go to memory_sync - distillation only at task completion.
            logger.info("should_after_execution: exec succeeded, going to perception")
            return "perception"

    # W2: fast-path failure - route to analysis for repo context
    # before retrying, rather than re-issuing the same failing tool call.
    # HR-4: Enforce per-failure retry cap for no-plan execution path.
    no_plan_fail_count = int(state.get("no_plan_fail_count") or 0) + 1
    if no_plan_fail_count >= 3:
        logger.warning(
            f"should_after_execution: no-plan fail count {no_plan_fail_count} >= 3, "
            "bailing to memory_sync"
        )
        return "memory_sync"
    logger.info(
        f"should_after_execution: no plan, execution failed (attempt {no_plan_fail_count}) "
        "— going to analysis (W2)"
    )
    return "analysis"


def should_after_execution_with_replan(
    state: AgentState,
) -> Literal[
    "perception", "analysis", "step_controller", "verification", "replan", "memory_sync"
]:
    """
    Decide routing after execution node with replan support and token budget checking.
    If replan_required is set, route to replan_node.
    If token budget at 85%, route to memory_update for compaction.
    Otherwise use standard execution routing.
    W12 budget check is delegated to should_after_execution.
    """
    # HR-10 fix: removed check_and_prepare_compaction() call from router.
    # Token budget checking is handled entirely in memory_update_node via
    # check_budget(state) which calls check_and_prepare_compaction() internally.
    # Having it in both places caused double-compaction and cooldown timer issues.

    replan_required = state.get("replan_required")
    if replan_required:
        # P1-3: Guard against unbounded replan cycles (cap at 5 attempts)
        replan_attempts = int(state.get("replan_attempts") or 0)
        if replan_attempts >= 5:
            logger.warning(
                f"should_after_execution_with_replan: replan_attempts={replan_attempts} >= 5, "
                "giving up replan and routing to memory_sync"
            )
            return "memory_sync"
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

    NOT WIRED IN compile_agent_graph() — the main graph uses a fixed edge
    verification → evaluation (evaluation_node handles the same routing with
    richer context). This function is kept for GraphFactory subgraphs that
    skip evaluation. Do not call from main graph code.

    Uses state["verification_passed"] as the authoritative truth (set by
    verification_node); falls back to checking all 6 result keys (Python +
    JS/TS) when the flag is absent.
    """
    debug_attempts: int = int(state.get("debug_attempts") or 0)
    max_debug_attempts: int = int(state.get("max_debug_attempts") or 3)

    # Use the authoritative flag when available (set by verification_node)
    _vp = state.get("verification_passed")
    if _vp is not None:
        passed = bool(_vp)
    else:
        # Recompute from result dict — check both Python and JS/TS keys
        verification_result = state.get("verification_result") or {}
        passed = True
        for key in ("tests", "linter", "syntax", "js_tests", "ts_check", "eslint"):
            r = verification_result.get(key, {})
            if isinstance(r, dict) and r.get("status") == "fail":
                passed = False
                break

    if passed:
        logger.info(
            "should_after_verification: verification passed, going to memory_sync"
        )
        return "memory_sync"

    # Verification failed — check debug attempts
    if debug_attempts < max_debug_attempts:
        logger.info(
            f"should_after_verification: failed, going to debug (attempt {debug_attempts + 1}/{max_debug_attempts})"
        )
        return "debug"

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
        # WR-2 fix: Guard against infinite loop when the current step has exhausted its
        # retry budget.  When should_after_step_controller sees retries >= MAX_STEP_RETRIES
        # it routes to verification instead of execution.  Verification passes (no side-
        # effect to verify), evaluation returns "replan" (plan incomplete) → step_controller
        # → same exhausted step → verification → evaluation → ... unbounded loop.
        # Fix: if the current step already has >= MAX_STEP_RETRIES recorded, route to debug
        # so a targeted fix can be generated, rather than looping.
        _MAX_STEP_RETRIES = 3
        _current_step = int(state.get("current_step") or 0)
        _step_retry_counts: dict = state.get("step_retry_counts") or {}
        _step_retries = int(_step_retry_counts.get(str(_current_step), 0))
        if _step_retries >= _MAX_STEP_RETRIES:
            logger.warning(
                f"should_after_evaluation: replan requested but step {_current_step} has "
                f"exhausted retries ({_step_retries}/{_MAX_STEP_RETRIES}) — routing to debug"
            )
            # Use the same total_debug_attempts guard as the "debug" branch above
            MAX_TOTAL_DEBUG = 9
            total_debug = int(state.get("total_debug_attempts") or 0)
            if total_debug >= MAX_TOTAL_DEBUG:
                logger.warning(
                    "should_after_evaluation: total_debug_attempts cap reached on replan→debug path"
                )
                return "memory_sync"
            return "debug"
        logger.info(
            "should_after_evaluation: more work needed, going to step_controller"
        )
        return "step_controller"
    elif evaluation_result == "debug":
        # W4: Global cap prevents alternating-error-type loops (3 types × 3 attempts = 9)
        MAX_TOTAL_DEBUG = 9
        total_debug = int(state.get("total_debug_attempts") or 0)
        if total_debug >= MAX_TOTAL_DEBUG:
            logger.warning(
                f"should_after_evaluation: total_debug_attempts={total_debug} >= "
                f"{MAX_TOTAL_DEBUG}, routing to memory_sync to prevent infinite loop"
            )
            return "memory_sync"
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
                # The outer guard (current_step < len) already confirmed there are
                # remaining steps — route directly to execution.
                # WR-4 fix: removed the inner redundant `if current_step < len`
                # check whose `return "verification"` branch was dead code (unreachable
                # since the outer guard is the same condition).
                logger.info(
                    f"should_after_step_controller: advancing to step {current_step + 1}, going to execution"
                )
                return "execution"
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

    WR-5: This intentionally re-calls _task_is_complex after analysis because
    analysis may have populated `relevant_files` (>3 entries triggers complexity)
    or `current_plan` (≥2 steps triggers complexity), so the classification may
    differ from the route_after_perception call that sent the task here.  The
    duplicate call is deliberate — not dead code.
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

    async def _wait_for_user(state: AgentState, config: RunnableConfig):
        from src.core.orchestration.graph.nodes.wait_for_user_node import (
            wait_for_user_node,
        )

        return await wait_for_user_node(state, config)

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
    workflow.add_node("wait_for_user", _wait_for_user)

    # 2. Define Flow
    workflow.set_entry_point("perception")

    # Phase 2.1: Fast-Path Routing
    # - Tool call + simple -> execution
    # - No next action + last result OK -> memory_sync (task complete)
    # - Otherwise -> analysis for context
    workflow.add_conditional_edges(
        "perception",
        route_after_perception,
        {
            "execution": "execution",
            "analysis": "analysis",
            "memory_sync": "memory_sync",
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

    def should_after_memory_sync(
        state: AgentState,
    ) -> Literal["perception", "delegation", "end"]:
        """
        Route after memory_sync (distill_context + background tasks).

        CF-4 fix: When evaluation_result == "complete" the task is done.
        Routing back to perception caused completed tasks to restart from scratch.
        The fix adds an "end" branch that is taken when the task is marked complete.

        Route to:
          - "end"        → task complete (evaluation_result == "complete")
          - "delegation" → pending delegations
          - "perception" → more work needed (task not yet complete)
        """
        # CF-4: Exit the graph when the task is complete.
        evaluation_result = state.get("evaluation_result") or ""
        if evaluation_result == "complete":
            logger.info("should_after_memory_sync: task complete, routing to END")
            return "end"

        delegations = state.get("delegations") or []
        if delegations:
            logger.info(
                f"should_after_memory_sync: {len(delegations)} delegations, "
                "routing to delegation"
            )
            return "delegation"
        logger.info("should_after_memory_sync: no delegations, routing to perception")
        return "perception"

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
_COMPILED_GRAPH = None


def _get_compiled_graph():
    """Return the cached compiled agent graph, compiling it on first call."""
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        _COMPILED_GRAPH = compile_agent_graph()
    return _COMPILED_GRAPH


def _reset_compiled_graph() -> None:
    """Reset the cached graph (for tests that need a fresh compile)."""
    global _COMPILED_GRAPH
    _COMPILED_GRAPH = None


def route_execution(
    state: AgentState,
) -> Literal[
    "wait_for_user",
    "step_controller",
    "replan",
    "analysis",
    "perception",
    "memory_sync",
]:
    """
    Route after execution node.

    Priority:
    1. Plan Mode approval pending → wait_for_user (plan gate)
    2. Preview Mode confirmation pending → wait_for_user (diff gate)
    3. replan_required set → replan (step was too large, needs splitting)
    4. No plan (fast-path mode):
       a. Read-only tool → memory_sync (task answered)
       b. Execution failed → analysis (W2: deeper context before retry)
       c. More rounds needed → perception
    5. Otherwise → step_controller (normal planned flow)

    WR-1 fix: When there's no current_plan, avoid going through the full
    step_controller → verification → evaluation → memory_sync → perception cycle
    for simple read-only tasks.
    CF-2 fix: Add replan_required and W2 (fail→analysis) branches so these paths
    are live in the main graph (they existed only in the dead should_after_execution*
    routers before this fix).
    """
    # Plan Mode: write tool was blocked — suspend until user approves plan
    if state.get("awaiting_plan_approval", False):
        logger.info("route_execution: plan approval pending, routing to wait_for_user")
        return "wait_for_user"

    # Preview Mode: diff preview generated — suspend until user confirms write
    if state.get("awaiting_user_input", False):
        logger.info("route_execution: awaiting user input, routing to wait_for_user")
        return "wait_for_user"

    # CF-2 fix: check replan_required BEFORE step_controller so oversized patches
    # are split immediately rather than hitting the full verification/evaluation loop.
    if state.get("replan_required"):
        logger.info(
            f"route_execution: replan_required={state['replan_required']!r}, routing to replan"
        )
        return "replan"

    # WR-1 fix: Check if we're in fast-path mode (no plan) and route accordingly.
    current_plan = state.get("current_plan") or []
    if not current_plan:
        last_tool = state.get("last_tool_name", "")
        read_only_tools = {
            "read_file",
            "grep",
            "glob",
            "find_symbol",
            "search_code",
            "list_directory",
        }
        last_result = state.get("last_result") or {}
        execution_failed = not last_result.get("ok", True)

        if last_tool in read_only_tools:
            # Read-only tool succeeded — answering a question, task is complete.
            logger.info(
                "route_execution: fast-path read-only tool, routing to memory_sync"
            )
            return "memory_sync"
        elif execution_failed and state.get("rounds", 0) >= 1:
            # CF-2 / W2 fix: execution failed on a fast-path (no plan) task.
            # Route to analysis for deeper context before the LLM retries, rather
            # than back to perception which has no additional retrieval.
            logger.info(
                "route_execution: fast-path execution failed, routing to analysis (W2)"
            )
            return "analysis"
        elif state.get("rounds", 0) >= 1:
            # More rounds needed but no failure — go back to perception.
            logger.info(
                "route_execution: fast-path with no plan, routing to perception"
            )
            return "perception"

    return "step_controller"


def route_after_wait_for_user(
    state: AgentState,
) -> Literal["execute", "perception", "planning"]:
    """
    Route after user confirms/rejects preview or approves/rejects plan.

    Plan Mode:
      - approved  → execute (write tools now unblocked)
      - rejected  → planning (re-plan with feedback)

    Preview Mode:
      - confirmed → execute (apply the diff)
      - rejected  → perception (abort and let agent continue)
    """
    # Plan Mode branch — awaiting_plan_approval was True before wait_for_user ran;
    # the node clears it and sets plan_mode_approved.
    plan_mode_approved = state.get("plan_mode_approved")
    if plan_mode_approved is not None:
        if plan_mode_approved:
            logger.info("route_after_wait_for_user: plan approved, resuming execution")
            return "execute"
        logger.info("route_after_wait_for_user: plan rejected, re-planning")
        return "planning"

    # Preview Mode branch
    confirmed = state.get("preview_confirmed", False)
    if confirmed:
        logger.info(
            "route_after_wait_for_user: preview confirmed, executing pending action"
        )
        return "execute"

    logger.info("route_after_wait_for_user: preview rejected, going to perception")
    return "perception"


def should_after_execution_with_compaction(
    state: AgentState,
) -> Literal[
    "perception",
    "analysis",
    "step_controller",
    "verification",
    "replan",
    "memory_sync",
    "execution",
    "wait_for_user",
]:
    """
    Check token budget AND tool budget for auto-compaction.

    Priority:
    1. awaiting_user_input → wait_for_user (Preview Mode)
    2. Tool budget exhausted → memory_sync
    3. Token budget at threshold → memory_sync (compact via distillation)
    4. Otherwise → normal routing
    """
    from src.core.orchestration.token_budget import get_token_budget_monitor

    awaiting = state.get("awaiting_user_input", False)
    if awaiting:
        return "wait_for_user"

    tool_call_count = int(state.get("tool_call_count") or 0)
    max_tool_calls = int(state.get("max_tool_calls") or 100)
    if tool_call_count >= max_tool_calls:
        logger.warning(
            f"should_after_execution_with_compaction: tool_call_count={tool_call_count} >= {max_tool_calls}, memory_sync"
        )
        return "memory_sync"

    # HR-10 fix: removed check_and_prepare_compaction() call from router.
    # Token budget checking is handled entirely in memory_update_node.
    return should_after_execution_with_replan(state)
