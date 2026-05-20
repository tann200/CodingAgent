import hashlib
import json
import logging
from typing import Any, Literal, Mapping

from src.core.orchestration.graph.perception_routing import (
    QUERY_TOOLS,
    READ_ONLY_TOOLS,
    _is_large_or_frontier,
    _is_nano_or_small,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOOL_CALLS = 30  # default budget for standard agent graph
_LOOP_GUARD_ROUNDS = 10  # round threshold for stuck-loop detection
_RECOVERY_CAPS: dict[str, int] = {
    "small": 4,
    "medium": 8,
    "large": 12,
    "frontier": 12,
}


def _is_success(result: Any) -> bool:
    """Simple result success check - did execution succeed?"""
    return bool(result and (result.get("ok") or result.get("status") == "ok"))


def should_after_execution(
    state: Mapping[str, Any],
) -> Literal[
    "perception", "analysis", "step_controller", "verification", "memory_sync", "replan"
]:
    """Decide routing after execution node."""
    if state.get("replan_required"):
        replan_attempts = int(state.get("replan_attempts") or 0)
        replan_cap = 3 if _is_large_or_frontier(state) else 5
        if replan_attempts >= replan_cap:
            logger.warning(
                f"should_after_execution: replan_attempts={replan_attempts} >= {replan_cap}, "
                "giving up replan and routing to memory_sync"
            )
            return "memory_sync"
        return "replan"

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

    execution_waves = state.get("execution_waves")
    current_wave = state.get("current_wave") or 0
    if execution_waves and current_wave < len(execution_waves):
        wave_step_ids = execution_waves[current_wave]
        logger.info(
            f"should_after_execution: wave {current_wave + 1}/{len(execution_waves)}, "
            f"steps={wave_step_ids}"
        )

    if current_plan and current_step < len(current_plan):
        if _is_success(last_result):
            next_step = current_step + 1
            if next_step < len(current_plan):
                logger.info(
                    f"should_after_execution: step completed, routing to step_controller for step {next_step + 1}/{len(current_plan)}"
                )
                return "step_controller"
            logger.info(
                "should_after_execution: all steps complete, going to verification"
            )
            return "verification"

        step_retry_counts: dict = state.get("step_retry_counts") or {}
        step_retries = int(step_retry_counts.get(str(current_step), 0))
        max_exec_step_retries = 3
        if step_retries >= max_exec_step_retries:
            logger.warning(
                f"should_after_execution: step {current_step} failed after "
                f"{step_retries} retries — bailing to analysis for fresh context"
            )
            return "analysis"
        logger.info("should_after_execution: step failed, going to perception")
        return "perception"

    if last_result is not None:
        if last_result.get("_completion_detected"):
            logger.info(
                "should_after_execution: completion signal detected via unregistered tool, "
                "routing to memory_sync"
            )
            return "memory_sync"

        execution_ok = last_result.get("ok") or last_result.get("status") == "ok"
        if execution_ok:
            task = (state.get("task") or "").lower()
            last_tool = state.get("last_tool_name")
            modification_keywords = (
                "add ",
                "prepend",
                "append",
                "edit ",
                "fix ",
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
                keyword in task for keyword in modification_keywords
            )
            if read_then_modify:
                logger.info(
                    "should_after_execution: read done, task implies mod, "
                    "going back to perception"
                )
                return "perception"

            read_tool = last_tool in ("read_file", "fs.read")
            task_implies_modification = any(
                keyword in task for keyword in modification_keywords
            )
            if read_tool and not task_implies_modification:
                logger.info(
                    "should_after_execution: read-only task complete, routing to memory_sync"
                )
                return "memory_sync"

            rounds = int(state.get("rounds") or 0)
            if rounds >= _LOOP_GUARD_ROUNDS:
                logger.info(
                    f"should_after_execution: no-plan success at rounds={rounds} "
                    "— forcing memory_sync to break perception loop"
                )
                return "memory_sync"
            logger.info("should_after_execution: exec succeeded, going to perception")
            return "perception"

    no_plan_fail_count = int(state.get("no_plan_fail_count") or 0)
    result_content = (last_result or {}).get("content") or (last_result or {}).get(
        "text", ""
    )
    result_error = (last_result or {}).get("error", "") or ""
    is_meaningful_result = bool(
        result_content
        and not result_error.lower().startswith("validation_error")
        and "format_error" not in result_error.lower()
    )

    if last_result is None:
        logger.info(
            "should_after_execution: no plan, no result yet - going to analysis for context"
        )
        return "analysis"

    if not is_meaningful_result:
        logger.info(
            f"should_after_execution: meaningless result, retrying "
            f"(error={result_error[:50] if result_error else 'empty'})"
        )
        return "perception"

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
    "perception",
    "analysis",
    "step_controller",
    "verification",
    "memory_sync",
    "replan",
]:
    """Backward-compatible pure delegator for should_after_execution."""
    return should_after_execution(state)  # type: ignore[return-value]


def should_after_execution_with_compaction(
    state: Mapping[str, Any],
) -> Literal[
    "perception",
    "analysis",
    "step_controller",
    "verification",
    "memory_sync",
    "replan",
]:
    """Backward-compatible pure delegator for should_after_execution."""
    return should_after_execution(state)  # type: ignore[return-value]


def should_after_verification(
    state: Mapping[str, Any],
) -> Literal["memory_sync", "debug", "end"]:
    """Decide routing after verification node."""
    debug_attempts = int(state.get("debug_attempts") or 0)
    max_debug_attempts = int(state.get("max_debug_attempts") or 3)

    verification_passed = state.get("verification_passed")
    if verification_passed is not None:
        passed = bool(verification_passed)
    else:
        verification_result = state.get("verification_result") or {}
        passed = True
        for key in ("tests", "linter", "syntax", "js_tests", "ts_check", "eslint"):
            result = verification_result.get(key, {})
            if isinstance(result, dict) and result.get("status") == "fail":
                passed = False
                break

    if passed:
        logger.info(
            "should_after_verification: verification passed, going to memory_sync"
        )
        return "memory_sync"

    if debug_attempts < max_debug_attempts:
        logger.info(
            f"should_after_verification: failed, going to debug (attempt {debug_attempts + 1}/{max_debug_attempts})"
        )
        return "debug"

    logger.warning(
        f"should_after_verification: max debug attempts reached ({max_debug_attempts}), routing to memory_sync for cleanup"
    )
    return "memory_sync"


def should_after_debug(
    state: Mapping[str, Any],
) -> Literal["execution", "memory_sync", "end"]:
    """Decide routing after debug node."""
    next_action = state.get("next_action")
    debug_attempts = int(state.get("debug_attempts") or 0)
    max_debug_attempts = int(state.get("max_debug_attempts") or 3)

    total_recovery = int(state.get("total_recovery_attempts") or 0)
    model_tier = (state.get("model_tier") or "medium").lower()
    recovery_cap = _RECOVERY_CAPS.get(model_tier, 8)
    if total_recovery >= recovery_cap:
        logger.warning(
            f"should_after_debug: global recovery cap ({recovery_cap}) reached "
            f"(total_recovery_attempts={total_recovery}, tier={model_tier}) — routing to memory_sync"
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
    """Decide routing after replan node."""
    total_recovery = int(state.get("total_recovery_attempts") or 0)
    model_tier = (state.get("model_tier") or "medium").lower()
    recovery_cap = _RECOVERY_CAPS.get(model_tier, 8)
    if total_recovery >= recovery_cap:
        logger.warning(
            f"should_after_replan: global recovery cap ({recovery_cap}) reached "
            f"(total_recovery_attempts={total_recovery}, tier={model_tier}) — routing to memory_sync"
        )
        return "memory_sync"

    if state.get("replan_required"):
        logger.info("should_after_replan: replan still required, going to perception")
        return "perception"

    logger.info("should_after_replan: replan complete, going to step_controller")
    return "step_controller"


def should_after_evaluation(
    state: Mapping[str, Any],
) -> Literal["memory_sync", "step_controller", "debug", "end"]:
    """Decide routing after evaluation node."""
    evaluation_result = state.get("evaluation_result", "complete")

    logger.info(f"should_after_evaluation: result={evaluation_result}")

    if evaluation_result == "complete":
        logger.info("should_after_evaluation: task complete, going to memory_sync")
        return "memory_sync"

    if evaluation_result == "replan":
        max_step_retries = 3
        current_step = int(state.get("current_step") or 0)
        step_retry_counts: dict = state.get("step_retry_counts") or {}
        step_retries = int(step_retry_counts.get(str(current_step), 0))
        if step_retries >= max_step_retries:
            logger.warning(
                f"should_after_evaluation: replan requested but step {current_step} has "
                f"exhausted retries ({step_retries}/{max_step_retries}) — routing to debug"
            )
            max_total_debug = 5 if _is_large_or_frontier(state) else 9
            total_debug = int(state.get("total_debug_attempts") or 0)
            if total_debug >= max_total_debug:
                logger.warning(
                    "should_after_evaluation: total_debug_attempts cap reached on replan→debug path"
                )
                return "memory_sync"
            return "debug"
        logger.info(
            "should_after_evaluation: more work needed, going to step_controller"
        )
        return "step_controller"

    if evaluation_result == "debug":
        max_total_debug = 5 if _is_large_or_frontier(state) else 9
        total_debug = int(state.get("total_debug_attempts") or 0)
        if total_debug >= max_total_debug:
            logger.warning(
                f"should_after_evaluation: total_debug_attempts={total_debug} >= "
                f"{max_total_debug}, routing to memory_sync to prevent infinite loop"
            )
            return "memory_sync"
        logger.info("should_after_evaluation: verification failed, going to debug")
        return "debug"

    logger.info("should_after_evaluation: ending task")
    return "end"


def _check_tool_budget(state: Mapping[str, Any]) -> bool:
    """Return True when the tool-call budget is exhausted."""
    tool_call_count = int(state.get("tool_call_count") or 0)
    max_tool_calls = int(state.get("max_tool_calls") or _DEFAULT_MAX_TOOL_CALLS)
    return tool_call_count >= max_tool_calls


def _check_plan_approval_pending(state: Mapping[str, Any]) -> bool:
    """Return True when a plan approval gate is outstanding."""
    return bool(state.get("awaiting_plan_approval", False))


def _check_preview_pending(state: Mapping[str, Any]) -> bool:
    """Return True when diff preview confirmation is outstanding."""
    return bool(state.get("awaiting_user_input", False))


def _check_replan_required(state: Mapping[str, Any]) -> str | None:
    """Evaluate the replan branch and return a destination or None."""
    if not state.get("replan_required"):
        return None

    replan_attempts = int(state.get("replan_attempts") or 0)
    replan_cap = 3 if _is_large_or_frontier(state) else 5
    if replan_attempts >= replan_cap:
        logger.warning(
            f"route_execution: replan_attempts={replan_attempts} >= {replan_cap}, "
            "giving up replan and routing to memory_sync"
        )
        return "memory_sync"

    current_plan_for_hash = state.get("current_plan") or []
    last_plan_hash = state.get("last_plan_hash")
    if last_plan_hash and current_plan_for_hash:
        try:
            current_plan_str = json.dumps(current_plan_for_hash, sort_keys=True, default=str)
            current_hash = hashlib.sha256(current_plan_str.encode()).hexdigest()
            if current_hash == last_plan_hash:
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
    """Evaluate the no-plan fast-path branch and return a destination or None."""
    current_plan = state.get("current_plan") or []
    if current_plan:
        return None

    last_tool = state.get("last_tool_name", "")
    read_only_tools = READ_ONLY_TOOLS
    last_result = state.get("last_result") or {}
    execution_failed = not (
        last_result.get("ok", False) or last_result.get("status") == "ok"
    )

    if last_result.get("_completion_detected"):
        logger.info(
            "route_execution: _completion_detected flag set — routing to memory_sync"
        )
        return "memory_sync"

    _query_tools = QUERY_TOOLS

    if last_tool in read_only_tools:
        if last_tool in ("read_file", "fs.read"):
            skip_read_then_modify = _is_nano_or_small(state)
            task_lower = (state.get("task") or "").lower()
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
                "fix ",
                "top of ",
                "beginning of ",
                "after ",
                "before ",
                "on top of ",
                "inside ",
                "contents of ",
            )
            if not skip_read_then_modify and any(
                keyword in task_lower for keyword in modification_keywords
            ):
                logger.info(
                    "route_execution: read_file done, task implies modification "
                    "— routing to perception for write step"
                )
                return "perception"
            if skip_read_then_modify and any(
                keyword in task_lower for keyword in modification_keywords
            ):
                logger.debug(
                    "route_execution: P3-A skipping read-then-modify heuristic "
                    "for constrained tier"
                )

        if last_tool in _query_tools:
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

    if execution_failed and state.get("rounds", 0) >= 1:
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

    if state.get("rounds", 0) >= _LOOP_GUARD_ROUNDS:
        logger.info(
            "route_execution: fast-path no-plan loop guard triggered "
            f"(rounds={state.get('rounds', 0)}), routing to memory_sync"
        )
        return "memory_sync"

    if state.get("rounds", 0) >= 1:
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
    """Route after execution node."""
    if "context_overflow" in (state.get("errors") or []):
        logger.warning(
            "route_execution: context overflow in errors — routing to memory_sync "
            "to end pipeline cleanly (history has been truncated)"
        )
        return "memory_sync"

    if _check_tool_budget(state):
        logger.warning(
            f"route_execution: tool budget exhausted "
            f"({state.get('tool_call_count', 0)}/{state.get('max_tool_calls', _DEFAULT_MAX_TOOL_CALLS)}), "
            "routing to memory_sync"
        )
        return "memory_sync"

    if _check_plan_approval_pending(state):
        logger.info("route_execution: plan approval pending, routing to wait_for_user")
        return "wait_for_user"

    if _check_preview_pending(state):
        logger.info("route_execution: awaiting user input, routing to wait_for_user")
        return "wait_for_user"

    replan_dest = _check_replan_required(state)
    if replan_dest is not None:
        return replan_dest  # type: ignore[return-value]

    fast_path_dest = _check_no_plan_fast_path(state)
    if fast_path_dest is not None:
        return fast_path_dest  # type: ignore[return-value]

    return "step_controller"
