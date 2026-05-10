import logging
from typing import Any, Literal, Mapping

from src.core.orchestration.graph.perception_routing import _is_large_or_frontier

logger = logging.getLogger(__name__)

_MAX_ROUNDS_PLANNING = 15  # force-end after this many planning rounds


def should_after_plan_validator(
    state: Mapping[str, Any],
) -> Literal["execute", "planning", "wait_for_user"]:
    """Decide routing after the plan_validator node."""
    plan_validation = state.get("plan_validation")
    action_failed = state.get("action_failed")
    rounds = int(state.get("rounds") or 0)
    plan_attempts = int(state.get("plan_attempts") or 0)

    logger.info(
        f"should_after_plan_validator: validation={plan_validation}, action_failed={action_failed}, rounds={rounds}, plan_attempts={plan_attempts}"
    )

    if state.get("plan_resumed", False):
        logger.info(
            "should_after_plan_validator: plan_resumed=True — skipping re-validation, executing"
        )
        return "execute"

    if _is_large_or_frontier(state):
        if state.get("plan_mode_enabled", False) and not state.get(
            "plan_mode_approved", False
        ):
            logger.info(
                "should_after_plan_validator: P3b-B capable tier + plan_mode — suspending for user approval"
            )
            return "wait_for_user"
        logger.info(
            "should_after_plan_validator: P3b-B capable tier — skipping validation, executing"
        )
        return "execute"

    if rounds >= 8:
        logger.warning(
            f"should_after_plan_validator: rounds={rounds} >= 8, forcing execution to break loop"
        )
        return "execute"

    if plan_attempts >= 3:
        logger.warning(
            f"should_after_plan_validator: plan_attempts={plan_attempts} >= 3, forcing execution"
        )
        return "execute"

    if action_failed or not plan_validation or not plan_validation.get("valid", False):
        logger.info("should_after_plan_validator: plan invalid, re-planning (F10)")
        return "planning"

    if state.get("plan_mode_enabled", False) and not state.get(
        "plan_mode_approved", False
    ):
        logger.info(
            "should_after_plan_validator: plan_mode enabled, suspending for user approval"
        )
        return "wait_for_user"

    logger.info("should_after_plan_validator: plan valid, executing")
    return "execute"


def should_after_planning(
    state: Mapping[str, Any],
) -> Literal["execute", "memory_sync", "end"]:
    """Backward-compatible router used by planner subgraphs."""
    if state.get("rounds", 0) >= _MAX_ROUNDS_PLANNING:
        return "end"
    if state.get("next_action"):
        return "execute"
    current_plan = state.get("current_plan")
    if current_plan and len(current_plan) > 0:
        return "execute"
    if state.get("last_result"):
        return "memory_sync"
    return "end"


def should_after_step_controller(
    state: Mapping[str, Any],
) -> Literal["execution", "verification"]:
    """Decide whether the next planned step should execute or verify."""
    current_plan = state.get("current_plan") or []
    current_step = int(state.get("current_step") or 0)
    last_result = state.get("last_result")

    logger.info(
        f"should_after_step_controller: current_step={current_step}, plan_len={len(current_plan)}, last_result={last_result}"
    )

    if current_plan and current_step < len(current_plan):
        if last_result and isinstance(last_result, dict):
            if last_result.get("ok"):
                logger.info(
                    f"should_after_step_controller: advancing to step {current_step + 1}/{len(current_plan)}, going to execution"
                )
                return "execution"

            max_step_retries = 3
            step_retry_counts: dict = state.get("step_retry_counts") or {}
            retries = int(step_retry_counts.get(str(current_step), 0))
            if retries >= max_step_retries:
                logger.warning(
                    f"should_after_step_controller: step {current_step + 1} retry budget ({max_step_retries}) exhausted, routing to verification (will trigger debug via evaluation)"
                )
                return "verification"
            logger.info(
                f"should_after_step_controller: step {current_step + 1} execution failed (retry {retries}/{max_step_retries}), going to execution"
            )
            return "execution"

        logger.info("should_after_step_controller: no last_result, going to execution")
        return "execution"

    logger.info("should_after_step_controller: no pending steps, going to verification")
    return "verification"
