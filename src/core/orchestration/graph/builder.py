import hashlib
import json
import logging
import threading
from typing import Any, Dict, Literal, Mapping

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

logger = logging.getLogger(__name__)

try:
    from src.core.orchestration.token_budget import (
        get_token_budget_monitor as _get_token_budget_monitor,
    )
except Exception:
    _get_token_budget_monitor = None  # type: ignore[assignment]

# D-11: Named routing constants — avoids magic numbers in router functions.
_MAX_ROUNDS_PLANNING = 15  # force-end after this many planning rounds
_DEFAULT_MAX_TOOL_CALLS = 30  # default budget for standard agent graph
_AUTONOMOUS_MAX_TOOL_CALLS = 100  # default budget for autonomous/full graph
_LOOP_GUARD_ROUNDS = 10  # round threshold for stuck-loop detection


def should_after_planning(
    state: Mapping[str, Any],
) -> Literal["execute", "memory_sync", "end"]:
    """
    Routing after planning node.

    NOT WIRED IN compile_agent_graph() — the main graph routes planning →
    plan_validator_node unconditionally.

    P2-E audit note: function is still used by graph_factory.py (two call sites
    at lines 56 and 73) for lightweight subgraph flavors that bypass
    plan_validator.  Not dead code; do not remove.
    """
    logger.info(
        f"should_after_planning: rounds={state.get('rounds')}, next_action={state.get('next_action')}, current_plan={state.get('current_plan')}"
    )
    if state.get("rounds", 0) >= _MAX_ROUNDS_PLANNING:
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
    state: Mapping[str, Any],
) -> Literal["execute", "planning", "wait_for_user"]:
    """
    Decide routing after plan_validator node.

    Valid plan + plan_mode_enabled (and not yet approved) → wait_for_user for approval.
    Valid plan (no plan mode, or already approved)        → execute directly.
    Invalid plan                                           → planning (F10 fix).
    Emergency loop guard (rounds >= 8)                    → execute to break cycle.
    Resumed plan (plan_resumed=True)                      → execute directly (CF-5 fix).

    P3b-B: LARGE/FRONTIER models skip plan_validator logic entirely.
    The validator was designed to catch hallucinated tool names and missing
    steps from small models. Capable 30B+ models produce structurally sound
    plans; the validation adds one extra LLM call with negligible value.
    plan_mode approval is still honoured even for capable tiers.
    """
    plan_validation = state.get("plan_validation")
    action_failed = state.get("action_failed")
    rounds = int(state.get("rounds") or 0)
    plan_attempts = int(state.get("plan_attempts") or 0)

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

    # P3b-B: Capable models (LARGE/FRONTIER) trust their own plan output.
    # Still honour plan_mode approval requirement.
    if _is_large_or_frontier(state):
        if state.get("plan_mode_enabled", False) and not state.get(
            "plan_mode_approved", False
        ):
            logger.info(
                "should_after_plan_validator: P3b-B capable tier + plan_mode — "
                "suspending for user approval"
            )
            return "wait_for_user"
        logger.info(
            "should_after_plan_validator: P3b-B capable tier — skipping validation, executing"
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


def should_use_prsw(state: Mapping[str, Any]) -> bool:
    """
    Determine if PRSW execution should be used.

    Returns True if:
    - Multiple delegations exist with mixed read/write roles
    - Or execution_waves has multiple waves with different step types
    """
    _delegations_raw = state.get("delegations")
    delegations: list = _delegations_raw if _delegations_raw is not None else []
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


def _task_is_complex(state: Mapping[str, Any]) -> bool:
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


def _task_has_more_steps(state: Mapping[str, Any]) -> bool:
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
        # WF-VOL23-2: removed r"\band\b" — matches virtually every English sentence,
        # causing spurious re-analysis on all compound-sentence tasks (e.g. "read and
        # summarize", "find and fix").  The remaining patterns are more discriminating.
        r"\bthen\b",  # "do this then do that"
        r"\bafter that\b",
        r"\bnext\b",
        r"\balso\b",
        r"\bas well\b",
        r",\s*\w+\s+and\s+\w+",  # "file1, file2 and file3"
    ]

    for pattern in multi_step_patterns:
        if _re.search(pattern, combined_task):
            tool_call_count = int(state.get("tool_call_count") or 0)
            if tool_call_count < 3:
                logger.info(
                    f"route_after_perception: multi-step task detected "
                    f"(pattern='{pattern}', tool_calls={tool_call_count})"
                )
                return True

    return False


def _is_large_or_frontier(state: Mapping[str, Any]) -> bool:
    """Return True when the active model tier is LARGE or FRONTIER.

    Used by P3b-A/B/E to bypass overhead nodes (analyst_delegation,
    plan_validator) that were calibrated for 7–9B failure modes and add
    pure latency for capable 30B+ models.
    """
    tier = (state.get("model_tier") or "").lower()
    return tier in ("large", "frontier")


def _is_nano_or_small(state: Mapping[str, Any]) -> bool:
    """Return True when the active model tier is NANO or SMALL.

    Used by P3-A to skip expensive overhead nodes (analysis, analyst_delegation)
    that add context pressure without improving outcomes on 1–9B models.
    """
    tier = (state.get("model_tier") or "").lower()
    return tier in ("nano", "small")


def route_after_perception(
    state: Mapping[str, Any],
) -> Literal["execution", "analysis", "memory_sync", "planning"]:
    """
    Phase 2.1: Fast-Path Routing.
    If perception generated a valid tool call and task is simple, go to execution.
    Complex tasks are forced through analysis.

    P2-A fix: simple tasks on their first round (no prior action, no prior
    rounds) now bypass the analysis node entirely and go directly to planning.
    This saves one analysis LLM call (~2k-5k tokens) for tasks that are
    clearly single-action (explanation, single-file read, single annotation,
    etc.).  The classification uses the `task_complexity` flag already set by
    perception_node, falling back to `_task_is_complex()`.

    P3b-A: LARGE/FRONTIER models skip analysis AND analyst_delegation entirely.
    These pipeline nodes were designed to compensate for 7–9B reasoning gaps;
    30B+ models produce better plans without the extra overhead. Complex tasks
    on capable models now route perception → planning directly.

    CRITICAL FIX: When next_action=None after successful execution:
    - If task appears to have more steps (multi-step detected), continue to analysis
    - Otherwise, route to memory_sync for final distillation.

    This ensures multi-step tasks like "create folder and file" continue executing
    instead of prematurely ending after the first tool call.
    """
    # CF-1: needs_clarification — perception_node asked the user a question.
    # Route to memory_sync to end the current turn; the TUI will display the
    # assistant question and wait for the user's next message.
    if state.get("needs_clarification"):
        logger.info("route_after_perception: needs_clarification=True — ending turn")
        return "memory_sync"

    # REACT-OVF: context overflow detected — history already truncated by perception_node.
    # Route to memory_sync to persist what we have and let the next user turn start fresh.
    if "context_overflow" in (state.get("errors") or []):
        logger.warning(
            "route_after_perception: context overflow in errors — routing to memory_sync "
            "to end the pipeline cleanly (history has been truncated)"
        )
        return "memory_sync"

    next_action = state.get("next_action")
    last_result = state.get("last_result")
    rounds = state.get("rounds", 0)
    _capable = _is_large_or_frontier(state)
    _constrained = _is_nano_or_small(state)

    logger.info(
        f"route_after_perception: next_action={next_action is not None}, "
        f"rounds={rounds}, capable_tier={_capable}, constrained_tier={_constrained}"
    )

    if next_action:
        # WF-1: Prefer the pre-computed task_complexity flag from perception_node.
        # Falls back to _task_is_complex() for state dicts that don't carry the flag
        # (e.g. resumed sessions, tests that don't go through perception_node).
        _tc = state.get("task_complexity")

        # P3-A: NANO/SMALL already generated a tool call — trust it and execute.
        # Analysis is an extra LLM call that exhausts the tiny context window; the
        # model has already decided what to do.
        if _constrained:
            logger.info(
                "route_after_perception: P3-A next_action on constrained tier — "
                "executing directly (skipping analysis)"
            )
            return "execution"

        # P3b-E: LARGE/FRONTIER + simple task + pre-computed action → direct execution.
        # Skips both analysis AND planning for the fastest possible path.
        if _capable and _tc == "simple":
            logger.info(
                "route_after_perception: P3b-E simple task on capable tier — "
                "direct execution (skipping analysis + planning)"
            )
            return "execution"

        if _tc == "complex":
            if _capable:
                # P3b-A: Skip analysis/analyst_delegation for capable models.
                # Route complex tasks straight to planning — the model is capable
                # enough to produce a good plan without the pre-analysis LLM call.
                logger.info(
                    "route_after_perception: P3b-A complex task on capable tier — "
                    "skipping analysis, going directly to planning"
                )
                return "planning"
            logger.info(
                "route_after_perception: complex task (flag) - overriding fast-path, "
                "going to analysis"
            )
            return "analysis"
        if _tc == "simple":
            logger.info(
                "route_after_perception: simple task (flag) - going to execution"
            )
            return "execution"
        # Flag absent — fall back to heuristic
        if _task_is_complex(state):
            if _capable:
                # P3b-A: Same skip for heuristic-complex on capable models.
                logger.info(
                    "route_after_perception: P3b-A complex task (heuristic) on capable tier — "
                    "skipping analysis, going directly to planning"
                )
                return "planning"
            logger.info(
                "route_after_perception: complex task - overriding fast-path, "
                "going to analysis"
            )
            return "analysis"
        logger.info(
            "route_after_perception: simple task fast-path - going to execution"
        )
        return "execution"

    if last_result is not None and rounds > 0:
        execution_ok = last_result.get("ok") or last_result.get("status") == "ok"
        if execution_ok:
            if _task_has_more_steps(state):
                logger.info(
                    "route_after_perception: task has more steps, continuing execution"
                )
                # P3-A: NANO/SMALL skip analysis even for multi-step continuation.
                if _constrained:
                    return "planning"
                return "analysis"
            logger.info("route_after_perception: task complete, going to memory_sync")
            return "memory_sync"

    # P2-A: On a fresh task (no prior rounds, no prior action) with an explicit
    # "simple" classification, bypass the analysis node entirely and go directly
    # to planning.  Analysis is an extra LLM call that adds nothing for tasks like
    # "what does this function do?" or "add a docstring to function X".
    # Only fire when rounds == 0 to avoid bypassing analysis on resumed tasks.
    if rounds == 0:
        _tc = state.get("task_complexity")
        if _tc == "simple":
            logger.info(
                "route_after_perception: simple task on first round — bypassing "
                "analysis, going directly to planning"
            )
            return "planning"
        # P3b-A: LARGE/FRONTIER always skip analysis on the first round.
        # P3-A: NANO/SMALL also skip analysis — it consumes context without benefit.
        if _capable or _constrained:
            logger.info(
                "route_after_perception: P3b-A/P3-A first round on %s tier — "
                "skipping analysis, going directly to planning",
                "capable" if _capable else "constrained",
            )
            return "planning"
        if _tc != "complex" and not _task_is_complex(state):
            logger.info(
                "route_after_perception: heuristic-simple task on first round — "
                "bypassing analysis, going directly to planning"
            )
            return "planning"

    logger.info("route_after_perception: no action yet, going to analysis")
    return "analysis"


def should_after_execution(
    state: Mapping[str, Any],
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

    NOT WIRED IN compile_agent_graph() — the live router is route_execution.
    P2-E audit note: used by should_after_execution_with_replan (which IS wired)
    and by GraphFactory subgraphs.  Not dead code; do not remove.
    """
    # W12: Enforce tool call budget
    tool_call_count = int(state.get("tool_call_count") or 0)
    max_tool_calls = int(state.get("max_tool_calls") or _DEFAULT_MAX_TOOL_CALLS)
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
        if last_result is not None:
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
            # Step failed — check retry budget before routing to perception.
            # If this step has already been retried too many times, bail to analysis
            # so the agent gets fresh context instead of looping on the same history.
            _step_retry_counts: dict = state.get("step_retry_counts") or {}
            _step_retries = int(_step_retry_counts.get(str(current_step), 0))
            _MAX_EXEC_STEP_RETRIES = 3
            if _step_retries >= _MAX_EXEC_STEP_RETRIES:
                logger.warning(
                    f"should_after_execution: step {current_step} failed after "
                    f"{_step_retries} retries — bailing to analysis for fresh context"
                )
                return "analysis"
            logger.info("should_after_execution: step failed, going to perception")
            return "perception"

    # No plan - check if execution succeeded
    if last_result is not None:
        # Completion detected via hallucinated tool (e.g. "respond") — bypass perception loop
        if last_result.get("_completion_detected"):
            logger.info(
                "should_after_execution: completion signal detected via unregistered tool, "
                "routing to memory_sync"
            )
            return "memory_sync"

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
            # Loop guard: if we've been through perception → execution multiple times
            # with successful results and no plan, the model may be generating tool
            # calls indefinitely (common with small models). Cap at 10 rounds.
            rounds = int(state.get("rounds") or 0)
            if rounds >= _LOOP_GUARD_ROUNDS:
                logger.info(
                    f"should_after_execution: no-plan success at rounds={rounds} "
                    "— forcing memory_sync to break perception loop"
                )
                return "memory_sync"
            logger.info("should_after_execution: exec succeeded, going to perception")
            return "perception"

    # W2: fast-path failure - route to analysis for repo context
    # before retrying, rather than re-issuing the same failing tool call.
    # HR-4: Enforce per-failure retry cap for no-plan execution path.
    # NOTE: execution_node already increments no_plan_fail_count in state (HR-4 fix);
    # read the already-updated value directly — do NOT add 1 here again.
    no_plan_fail_count = int(state.get("no_plan_fail_count") or 0)
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
    state: Mapping[str, Any],
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
        # P1-3: Guard against unbounded replan cycles.
        # P3b-D: Cap is lower (3) for LARGE/FRONTIER — capable models that
        # need 3+ replans are stuck, not approaching a solution.
        replan_attempts = int(state.get("replan_attempts") or 0)
        _replan_cap = 3 if _is_large_or_frontier(state) else 5
        if replan_attempts >= _replan_cap:
            logger.warning(
                f"should_after_execution_with_replan: replan_attempts={replan_attempts} >= {_replan_cap}, "
                "giving up replan and routing to memory_sync"
            )
            return "memory_sync"
        logger.info(
            f"should_after_execution_with_replan: replan required - {replan_required}"
        )
        return "replan"

    return should_after_execution(state)


def should_after_verification(
    state: Mapping[str, Any],
) -> Literal["memory_sync", "debug", "end"]:
    """
    Decide routing after verification node.
    If verification passed, go to memory_sync.
    If verification failed and debug attempts remain, go to debug.
    Otherwise end.

    NOT WIRED IN compile_agent_graph() — the main graph uses a fixed edge
    verification → evaluation (evaluation_node handles the same routing with
    richer context).
    P2-E audit note: used by GraphFactory subgraphs that skip evaluation, and
    referenced by regression tests.  Not dead code; do not remove.

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
    state: Mapping[str, Any],
) -> Literal["execution", "memory_sync", "end"]:
    """
    Decide routing after debug node.
    If debug generated a fix action, go to execution.
    Otherwise go to memory_sync or end.
    """
    next_action = state.get("next_action")
    debug_attempts: int = int(state.get("debug_attempts") or 0)
    max_debug_attempts: int = int(state.get("max_debug_attempts") or 3)

    # P2-A: Global recovery cap — tier-aware hard stop across debug + replan combined.
    # Prevents unbounded loops when errors alternate between types, resetting individual
    # counters while total_recovery_attempts keeps growing.
    _total_recovery = int(state.get("total_recovery_attempts") or 0)
    _model_tier = (state.get("model_tier") or "medium").lower()
    _RECOVERY_CAPS = {"nano": 2, "small": 4, "medium": 8, "large": 12, "frontier": 12}
    _recovery_cap = _RECOVERY_CAPS.get(_model_tier, 8)
    if _total_recovery >= _recovery_cap:
        logger.warning(
            f"should_after_debug: global recovery cap ({_recovery_cap}) reached "
            f"(total_recovery_attempts={_total_recovery}, tier={_model_tier}) — routing to memory_sync"
        )
        return "memory_sync"

    if next_action:
        logger.info("should_after_debug: fix generated, going to execution")
        return "execution"

    if debug_attempts >= max_debug_attempts:
        logger.info("should_after_debug: max attempts, going to memory_sync")
        return "memory_sync"

    logger.info("should_after_debug: no fix generated, ending")
    return "end"


def should_after_replan(
    state: Mapping[str, Any],
) -> Literal["step_controller", "perception", "memory_sync"]:
    """
    Decide routing after replan node.
    After splitting oversized steps, go back to step_controller to execute the new smaller steps.
    """
    # P2-A: Global recovery cap check (mirrors should_after_debug).
    _total_recovery = int(state.get("total_recovery_attempts") or 0)
    _model_tier = (state.get("model_tier") or "medium").lower()
    _RECOVERY_CAPS = {"nano": 2, "small": 4, "medium": 8, "large": 12, "frontier": 12}
    _recovery_cap = _RECOVERY_CAPS.get(_model_tier, 8)
    if _total_recovery >= _recovery_cap:
        logger.warning(
            f"should_after_replan: global recovery cap ({_recovery_cap}) reached "
            f"(total_recovery_attempts={_total_recovery}, tier={_model_tier}) — routing to memory_sync"
        )
        return "memory_sync"

    replan_required = state.get("replan_required")
    if replan_required:
        # Replan still has an issue, go to perception for help
        logger.info("should_after_replan: replan still required, going to perception")
        return "perception"

    # Replan successful, continue with step controller
    logger.info("should_after_replan: replan complete, going to step_controller")
    return "step_controller"


def should_after_evaluation(
    state: Mapping[str, Any],
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
            # Use the same total_debug_attempts guard as the "debug" branch above.
            # P3b-D: Capable models fail faster — 5 debug attempts vs 9 for small models.
            MAX_TOTAL_DEBUG = 5 if _is_large_or_frontier(state) else 9
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
        # W4: Global cap prevents alternating-error-type loops.
        # P3b-D: Lower cap (5) for LARGE/FRONTIER — capable models that hit 5 debug
        # cycles are genuinely stuck, not converging toward a solution.
        MAX_TOTAL_DEBUG = 5 if _is_large_or_frontier(state) else 9
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
    state: Mapping[str, Any],
) -> Literal["execution", "verification"]:
    """
    Step controller decides whether to proceed to execution or skip to verification.
    """
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
                    f"should_after_step_controller: advancing to step {current_step + 1}/{len(current_plan)}, going to execution"
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
    state: Mapping[str, Any],
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

    P3-A: NANO/SMALL models skip analyst_delegation — the subagent adds one more
    LLM call that small models can't use effectively. Route directly to planning.
    """
    # P3-A: Constrained models can't effectively process analyst_delegation output.
    if _is_nano_or_small(state):
        logger.info(
            "should_after_analysis: P3-A constrained tier — skipping analyst_delegation, "
            "going directly to planning"
        )
        return "planning"

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
    async def _perception(state: StateLike, config: RunnableConfig):
        return await perception_node(state, config)

    async def _analysis(state: StateLike, config: RunnableConfig):
        return await analysis_node(state, config)

    async def _planning(state: StateLike, config: RunnableConfig):
        return await planning_node(state, config)

    async def _execution(state: StateLike, config: RunnableConfig):
        return await execution_node(state, config)

    async def _step_controller(state: StateLike, config: RunnableConfig):
        return await step_controller_node(state, config)

    async def _verification(state: StateLike, config: RunnableConfig):
        return await verification_node(state, config)

    async def _debug(state: StateLike, config: RunnableConfig):
        return await debug_node(state, config)

    async def _memory_sync(state: StateLike, config: RunnableConfig):
        return await memory_update_node(state, config)

    async def _replan(state: StateLike, config: RunnableConfig):
        return await replan_node(state, config)

    async def _evaluation(state: StateLike, config: RunnableConfig):
        return await evaluation_node(state, config)

    async def _plan_validator(state: StateLike, config: RunnableConfig):
        return await plan_validator_node(state, config)

    async def _delegation(state: StateLike, config: RunnableConfig):
        return await delegation_node(state, config)

    async def _analyst_delegation(state: StateLike, config: RunnableConfig):
        return await analyst_delegation_node(state, config)

    async def _wait_for_user(state: StateLike, config: RunnableConfig):
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
        state: Mapping[str, Any],
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

        # CF-1: Exit when perception asked a clarifying question — the user has
        # not yet responded, so we must stop the current graph invocation here.
        if state.get("needs_clarification"):
            logger.info(
                "should_after_memory_sync: needs_clarification=True, routing to END "
                "to wait for user response"
            )
            return "end"

        # Fast-path completion: no plan, last execution succeeded, no pending action,
        # and at least one round completed. This covers simple read-only tasks (e.g.
        # "list files") that never go through evaluation_node to set evaluation_result.
        # Without this check the graph loops: memory_sync → perception → memory_sync.
        current_plan = state.get("current_plan") or []
        next_action = state.get("next_action")
        last_result = state.get("last_result") or {}
        execution_ok = last_result.get("ok") or last_result.get("status") == "ok"
        rounds = int(state.get("rounds") or 0)
        if not current_plan and not next_action and execution_ok and rounds > 0:
            logger.info(
                "should_after_memory_sync: fast-path task complete "
                f"(rounds={rounds}, no plan, no pending action) — routing to END"
            )
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
# MED-13 fix: guard with a lock so concurrent threads don't each compile the graph
# and then race to store the result — only one compilation happens.
_COMPILED_GRAPH = None
_COMPILED_GRAPH_LOCK = threading.Lock()

# TASK-6: Per-tier graph cache.  Keys: "standard", "frontier".
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
    workflow.add_node("frontier_loop", _frontier_loop)
    workflow.add_node("verification", _verification)
    workflow.add_node("evaluation", _evaluation)
    workflow.add_node("debug", _debug)
    workflow.add_node("memory_sync", _memory_sync)
    workflow.add_node("delegation", _delegation)
    workflow.add_node("wait_for_user", _wait_for_user)

    workflow.set_entry_point("perception")

    # perception → frontier_loop (always for this graph — tier is already frontier)
    # Still honour context overflow and clarification exits from perception.
    def _route_perception_frontier(
        state: Mapping[str, Any],
    ) -> Literal["frontier_loop", "memory_sync"]:
        if state.get("needs_clarification"):
            return "memory_sync"
        if "context_overflow" in (state.get("errors") or []):
            return "memory_sync"
        return "frontier_loop"

    workflow.add_conditional_edges(
        "perception",
        _route_perception_frontier,
        {"frontier_loop": "frontier_loop", "memory_sync": "memory_sync"},
    )

    # frontier_loop → verification | memory_sync | wait_for_user
    def _route_frontier_loop_exit(
        state: Mapping[str, Any],
    ) -> Literal["verification", "memory_sync", "wait_for_user"]:
        if state.get("awaiting_plan_approval"):
            return "wait_for_user"
        if "context_overflow" in (state.get("errors") or []):
            return "memory_sync"
        last_result = state.get("last_result")
        if last_result is None:
            # No tools were called — pure LLM response, task answered
            return "memory_sync"
        return "verification"

    workflow.add_conditional_edges(
        "frontier_loop",
        _route_frontier_loop_exit,
        {
            "verification": "verification",
            "memory_sync": "memory_sync",
            "wait_for_user": "wait_for_user",
        },
    )

    # wait_for_user → frontier_loop (resume after plan approval)
    def _route_wait_frontier(
        state: Mapping[str, Any],
    ) -> Literal["frontier_loop", "memory_sync"]:
        if state.get("plan_mode_approved"):
            return "frontier_loop"
        return "memory_sync"

    workflow.add_conditional_edges(
        "wait_for_user",
        _route_wait_frontier,
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
    def _route_debug_frontier(
        state: Mapping[str, Any],
    ) -> Literal["frontier_loop", "memory_sync", "end"]:
        r = should_after_debug(state)
        if r == "execution":
            return "frontier_loop"
        return r  # type: ignore[return-value]

    workflow.add_conditional_edges(
        "debug",
        _route_debug_frontier,
        {"frontier_loop": "frontier_loop", "memory_sync": "memory_sync", "end": END},
    )

    # memory_sync → delegation | perception | end
    def _should_after_memory_sync_frontier(
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

    workflow.add_conditional_edges(
        "memory_sync",
        _should_after_memory_sync_frontier,
        {"delegation": "delegation", "perception": "perception", "end": END},
    )

    workflow.add_edge("delegation", END)

    return workflow.compile()


def build_tier_graph(tier: str):
    """Return a compiled LangGraph graph appropriate for *tier*.

    Parameters
    ----------
    tier:
        Model tier string (e.g. ``"frontier"``, ``"large"``, ``"medium"``).
        Case-insensitive.  ``"large"`` and ``"frontier"`` get the
        ``frontier_loop_node`` graph; all others get the standard graph.

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
    _tier = (tier or "").lower()
    cache_key = "frontier" if _tier in ("large", "frontier") else "standard"

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
        if cache_key == "frontier":
            compiled = _compile_frontier_graph()
        else:
            compiled = compile_agent_graph()
        _GRAPH_CACHE[cache_key] = compiled
        logger.info("build_tier_graph: '%s' graph compiled and cached", cache_key)
        return compiled


def _check_tool_budget(state: Mapping[str, Any]) -> bool:
    """ARCH-3: Return True when the tool-call budget is exhausted (W12)."""
    tool_call_count = int(state.get("tool_call_count") or 0)
    max_tool_calls = int(state.get("max_tool_calls") or _DEFAULT_MAX_TOOL_CALLS)
    return tool_call_count >= max_tool_calls


def _check_plan_approval_pending(state: Mapping[str, Any]) -> bool:
    """ARCH-3: Return True when a Plan Mode write-gate approval is outstanding."""
    return bool(state.get("awaiting_plan_approval", False))


def _check_preview_pending(state: Mapping[str, Any]) -> bool:
    """ARCH-3: Return True when a diff-preview confirmation is awaited."""
    return bool(state.get("awaiting_user_input", False))


def _check_replan_required(
    state: Mapping[str, Any],
) -> str | None:
    """ARCH-3: Evaluate the replan branch; return destination or None to continue.

    Returns:
        ``"memory_sync"`` — attempts cap reached or plan diverged.
        ``"replan"``      — replan is required and safe to proceed.
        ``None``          — replan_required is falsy; caller should continue.
    """
    if not state.get("replan_required"):
        return None
    replan_attempts = int(state.get("replan_attempts") or 0)
    # P3b-D: Capable models that need 3+ replans are stuck; fail faster.
    _replan_cap = 3 if _is_large_or_frontier(state) else 5
    if replan_attempts >= _replan_cap:
        logger.warning(
            f"route_execution: replan_attempts={replan_attempts} >= {_replan_cap}, "
            "giving up replan and routing to memory_sync"
        )
        return "memory_sync"
    # WF-4: Plan divergence detection
    current_plan_for_hash = state.get("current_plan") or []
    last_plan_hash = state.get("last_plan_hash")
    if last_plan_hash and current_plan_for_hash:
        try:
            _cur_str = json.dumps(current_plan_for_hash, sort_keys=True, default=str)
            _cur_hash = hashlib.sha256(_cur_str.encode()).hexdigest()
            if _cur_hash == last_plan_hash:
                logger.warning(
                    "route_execution: WF-4 plan divergence detected — "
                    "new plan identical to last replan output, routing to memory_sync"
                )
                return "memory_sync"
        except Exception:
            pass
    logger.info(
        f"route_execution: replan_required={state['replan_required']!r}, routing to replan"
    )
    return "replan"


def _check_no_plan_fast_path(state: Mapping[str, Any]) -> str | None:
    """ARCH-3: Evaluate the no-plan fast-path branch; return destination or None.

    Returns a routing string when in fast-path mode (no current_plan), or
    ``None`` when a plan exists (caller should fall through to ``step_controller``).
    """
    current_plan = state.get("current_plan") or []
    if current_plan:
        return None

    last_tool = state.get("last_tool_name", "")
    read_only_tools = {
        # canonical names
        "read_file",
        "grep",
        "glob",
        "find_symbol",
        "search_code",
        "list_files",
        "fs.read",
        "fs.list",
        # common aliases / additional read-only tools
        "ls",
        "cat",
        "read",
        "find",
        "find_files",
        "rg",
        "bash_readonly",
        "git_status",
        "git_diff",
        "git_log",
        "batched_file_read",
        "read_file_bytes",
        "read_file_chunk",
        "ast_list_symbols",
        "find_references",
        "memory_search",
        "web_search",
        "fetch",
        "browse",
    }
    last_result = state.get("last_result") or {}
    execution_failed = not (
        last_result.get("ok", False) or last_result.get("status") == "ok"
    )

    if last_result.get("_completion_detected"):
        logger.info(
            "route_execution: _completion_detected flag set — routing to memory_sync"
        )
        return "memory_sync"

    # Query tools: the model fetched data in order to answer the user's question.
    # Give the model one more perception turn so it can interpret results and
    # respond in natural language rather than silently ending with "✓ Done".
    _query_tools = {
        "glob",
        "find",
        "find_files",
        "grep",
        "rg",
        "search_code",
        "find_symbol",
        "find_references",
        "memory_search",
        "web_search",
        "fetch",
        "browse",
    }

    if last_tool in read_only_tools:
        if last_tool in ("read_file", "fs.read"):
            # P3-A: Skip read-then-modify heuristic for NANO/SMALL.
            # On small models this causes loops: NANO reads → heuristic fires →
            # NANO reads the same file again → infinite loop.  Trust the model
            # to issue a write call on its own; don't force a perception round-trip.
            _skip_rtm = _is_nano_or_small(state)
            _task_lower = (state.get("task") or "").lower()
            _mod_kws = (
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
                "fix ",
                "top of ",
                "beginning of ",
                "after ",
                "before ",
                "on top of ",
                "inside ",
                "contents of ",
            )
            if not _skip_rtm and any(kw in _task_lower for kw in _mod_kws):
                logger.info(
                    "route_execution: read_file done, task implies modification "
                    "— routing to perception for write step"
                )
                return "perception"
            elif _skip_rtm and any(kw in _task_lower for kw in _mod_kws):
                logger.debug(
                    "route_execution: P3-A skipping read-then-modify heuristic "
                    "for constrained tier"
                )

        if last_tool in _query_tools:
            # Query tool returned results — route to perception so the model can
            # synthesise a natural language answer instead of silently ending.
            # Guard against perception loops: only allow one extra interpretation turn.
            rounds = int(state.get("rounds") or 0)
            if rounds < _LOOP_GUARD_ROUNDS:
                logger.info(
                    f"route_execution: query tool '{last_tool}' returned results "
                    "— routing to perception for interpretation turn"
                )
                return "perception"
            logger.info(
                f"route_execution: query tool '{last_tool}', loop guard "
                f"(rounds={rounds}) — routing to memory_sync"
            )
            return "memory_sync"

        logger.info("route_execution: fast-path read-only tool, routing to memory_sync")
        return "memory_sync"
    elif execution_failed and state.get("rounds", 0) >= 1:
        no_plan_fail_count = int(state.get("no_plan_fail_count") or 0)
        if no_plan_fail_count >= 3:
            logger.warning(
                f"route_execution: no_plan_fail_count={no_plan_fail_count} >= 3, "
                "bailing to memory_sync"
            )
            return "memory_sync"
        logger.info(
            f"route_execution: fast-path execution failed "
            f"(attempt {no_plan_fail_count + 1}), routing to analysis (W2)"
        )
        return "analysis"
    elif state.get("rounds", 0) >= _LOOP_GUARD_ROUNDS:
        logger.info(
            "route_execution: fast-path no-plan loop guard triggered "
            f"(rounds={state.get('rounds', 0)}), routing to memory_sync"
        )
        return "memory_sync"
    elif state.get("rounds", 0) >= 1:
        logger.info("route_execution: fast-path with no plan, routing to perception")
        return "perception"
    return None


def route_execution(
    state: Mapping[str, Any],
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
    1. W12: Tool call budget exhausted → memory_sync
    2. Plan Mode approval pending → wait_for_user (plan gate)
    3. Preview Mode confirmation pending → wait_for_user (diff gate)
    4. replan_required set → replan (P1-3: capped at 5 attempts → memory_sync)
    5. No plan (fast-path mode):
       a. _completion_detected flag → memory_sync (hallucinated terminal tool)
       b. Query tool (glob, grep, search_code, etc.) → perception (interpretation turn)
       c. Other read-only tool → memory_sync (task answered)
       d. Execution failed (HR-4: capped at 3 failures → memory_sync) → analysis
       e. rounds >= 10 loop guard → memory_sync
       f. More rounds needed → perception
    6. Otherwise → step_controller (normal planned flow)

    WR-1 fix: When there's no current_plan, avoid going through the full
    step_controller → verification → evaluation → memory_sync → perception cycle
    for simple read-only tasks.
    CF-2 fix: Add replan_required and W2 (fail→analysis) branches so these paths
    are live in the main graph (they existed only in the dead should_after_execution*
    routers before this fix).
    W12 / HR-4 / P1-3: Ported from dead should_after_execution* routers.
    ARCH-3: Delegates to helper sub-routers for each logical section.
    """
    # Gate 0: REACT-OVF — context overflow detected by execution_node (or perception_node
    # on a prior turn that was not yet flushed).  Route directly to memory_sync to end
    # the pipeline cleanly; history has already been truncated by the emitting node.
    if "context_overflow" in (state.get("errors") or []):
        logger.warning(
            "route_execution: context overflow in errors — routing to memory_sync "
            "to end pipeline cleanly (history has been truncated)"
        )
        return "memory_sync"

    # Gate 1: Tool budget
    if _check_tool_budget(state):
        logger.warning(
            f"route_execution: tool budget exhausted "
            f"({state.get('tool_call_count', 0)}/{state.get('max_tool_calls', _DEFAULT_MAX_TOOL_CALLS)}), "
            "routing to memory_sync"
        )
        return "memory_sync"

    # Gate 2 & 3: User-input suspension
    if _check_plan_approval_pending(state):
        logger.info("route_execution: plan approval pending, routing to wait_for_user")
        return "wait_for_user"

    if _check_preview_pending(state):
        logger.info("route_execution: awaiting user input, routing to wait_for_user")
        return "wait_for_user"

    # Gate 4: Replan branch
    replan_dest = _check_replan_required(state)
    if replan_dest is not None:
        return replan_dest  # type: ignore[return-value]

    # Gate 5: No-plan fast-path
    fast_path_dest = _check_no_plan_fast_path(state)
    if fast_path_dest is not None:
        return fast_path_dest  # type: ignore[return-value]

    return "step_controller"


def route_after_wait_for_user(
    state: Mapping[str, Any],
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
    state: Mapping[str, Any],
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
    awaiting = state.get("awaiting_user_input", False)
    if awaiting:
        return "wait_for_user"

    tool_call_count = int(state.get("tool_call_count") or 0)
    max_tool_calls = int(state.get("max_tool_calls") or _AUTONOMOUS_MAX_TOOL_CALLS)
    if tool_call_count >= max_tool_calls:
        logger.warning(
            f"should_after_execution_with_compaction: tool_call_count={tool_call_count} >= {max_tool_calls}, memory_sync"
        )
        return "memory_sync"

    # HR-10 fix: removed check_and_prepare_compaction() call from router.
    # Token budget checking is handled entirely in memory_update_node.
    return should_after_execution_with_replan(state)
