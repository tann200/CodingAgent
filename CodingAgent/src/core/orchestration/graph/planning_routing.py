import logging
from typing import Any, Literal, Mapping

from src.core.orchestration.graph.perception_routing import _is_large_or_frontier

logger = logging.getLogger(__name__)

_MAX_ROUNDS_PLANNING = 15  # force-end after this many planning rounds


def should_after_plan_validator(
    state: Mapping[str, Any],
) -> Literal["execute", "planning", "wait_for_user"]:
    """Decide routing after the plan_validator node.

    Safety invariants (Task #6):
    - Approval-sensitive routes NEVER select "execute" unless plan_mode_approved is
      actually True; approval checks run BEFORE forced-execute (rounds/plan_attempts)
      branches.
    - plan_resumed and large-tier shortcuts no longer bypass mandatory plan approval;
      they only skip *re-validation* when approval has already been granted.
    """
    plan_validation = state.get("plan_validation")
    action_failed = state.get("action_failed")
    rounds = int(state.get("rounds") or 0)
    plan_attempts = int(state.get("plan_attempts") or 0)
    plan_mode_enabled = bool(state.get("plan_mode_enabled", False))
    plan_mode_approved = bool(state.get("plan_mode_approved", False))

    logger.info(
        f"should_after_plan_validator: validation={plan_validation}, "
        f"action_failed={action_failed}, rounds={rounds}, "
        f"plan_attempts={plan_attempts}, plan_mode_enabled={plan_mode_enabled}, "
        f"plan_mode_approved={plan_mode_approved}"
    )

    # ── 1. Approval gate — always checked first ────────────────────────────
    # If plan_mode is enabled and the plan has NOT been approved yet, we must
    # suspend regardless of tier, plan_resumed, rounds, or plan_attempts.
    if plan_mode_enabled and not plan_mode_approved:
        logger.info(
            "should_after_plan_validator: plan_mode enabled but not approved — "
            "suspending for user approval (approval gate wins over all shortcuts)"
        )
        return "wait_for_user"

    # ── 2. plan_resumed shortcut — only valid when plan is already approved ─
    # (Approval was checked above; if we reach here and plan_resumed is set,
    # approval was either not required or already granted.)
    if state.get("plan_resumed", False):
        logger.info(
            "should_after_plan_validator: plan_resumed=True (approval satisfied) "
            "— skipping re-validation, executing"
        )
        return "execute"

    # ── 3. Large / frontier tier shortcut ─────────────────────────────────
    # Capable-tier models skip validation noise, but only when approval is not
    # pending (already handled above).
    if _is_large_or_frontier(state):
        logger.info(
            "should_after_plan_validator: capable tier (approval satisfied) "
            "— skipping validation, executing"
        )
        return "execute"

    # ── 4. Forced-execute escape hatches (rounds / plan_attempts) ─────────
    # These only fire after the approval gate has been cleared.
    if rounds >= 8:
        logger.warning(
            f"should_after_plan_validator: rounds={rounds} >= 8, "
            "forcing execution to break loop"
        )
        return "execute"

    if plan_attempts >= 3:
        logger.warning(
            f"should_after_plan_validator: plan_attempts={plan_attempts} >= 3, "
            "forcing execution"
        )
        return "execute"

    # ── 5. Plan validity check ─────────────────────────────────────────────
    if action_failed or not plan_validation or not plan_validation.get("valid", False):
        logger.info("should_after_plan_validator: plan invalid, re-planning (F10)")
        return "planning"

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
) -> Literal["execution", "verification", "planning", "end"]:
    """Decide the next node after step_controller.

    P3-T1: Extended to cover all routing cases:
    - cancel or plan exhausted → "end"
    - no plan yet              → "planning"
    - step pending / retry     → "execution"
    - step completed           → "verification"
    """
    # Cancellation takes highest priority
    cancel_event = state.get("cancel_event")
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("should_after_step_controller: canceled, routing to end")
        return "end"

    current_plan = state.get("current_plan") or []
    current_step = int(state.get("current_step") or 0)
    last_result = state.get("last_result")

    logger.info(
        f"should_after_step_controller: current_step={current_step}, plan_len={len(current_plan)}, last_result={last_result}"
    )

    # No plan → go generate one
    if not current_plan:
        logger.info("should_after_step_controller: no plan, routing to planning")
        return "planning"

    # Plan exhausted → go to verification / wrap-up
    if current_step >= len(current_plan):
        logger.info(
            "should_after_step_controller: plan exhausted (%d/%d), routing to verification",
            current_step,
            len(current_plan),
        )
        return "verification"

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
                f"should_after_step_controller: step {current_step + 1} retry budget ({max_step_retries}) exhausted, routing to verification"
            )
            return "verification"
        logger.info(
            f"should_after_step_controller: step {current_step + 1} execution failed (retry {retries}/{max_step_retries}), going to execution"
        )
        return "execution"

    logger.info("should_after_step_controller: no last_result, going to execution")
    return "execution"
