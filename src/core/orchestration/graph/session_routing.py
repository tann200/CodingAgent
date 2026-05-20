import logging
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)


def route_after_wait_for_user(
    state: Mapping[str, Any],
) -> Literal["execute", "perception", "planning"]:
    """Route after user confirms/rejects preview or approves/rejects a plan."""
    plan_mode_approved = state.get("plan_mode_approved")
    if plan_mode_approved is not None:
        if plan_mode_approved:
            logger.info("route_after_wait_for_user: plan approved, resuming execution")
            return "execute"
        logger.info("route_after_wait_for_user: plan rejected, re-planning")
        return "planning"

    confirmed = state.get("preview_confirmed", False)
    if confirmed:
        logger.info(
            "route_after_wait_for_user: preview confirmed, executing pending action"
        )
        return "execute"

    logger.info("route_after_wait_for_user: preview rejected, going to perception")
    return "perception"


def should_after_memory_sync(
    state: Mapping[str, Any],
) -> Literal["perception", "delegation", "end"]:
    """Route after memory_sync."""
    evaluation_result = state.get("evaluation_result") or ""
    if evaluation_result == "complete":
        logger.info("should_after_memory_sync: task complete, routing to END")
        return "end"

    if state.get("needs_clarification"):
        logger.info(
            "should_after_memory_sync: needs_clarification=True, routing to END "
            "to wait for user response"
        )
        return "end"

    current_plan = state.get("current_plan") or []
    next_action = state.get("next_action")
    last_result = state.get("last_result") or {}
    execution_ok = last_result.get("ok") or last_result.get("status") == "ok"
    rounds = int(state.get("rounds") or 0)
    # Fast-path to END: breaks the memory_sync→perception→memory_sync loop for
    # simple no-plan tasks (e.g. "list all files") where verification/evaluation
    # nodes are never entered.
    # F-08 guard: do NOT fire the fast-path when evaluation_result indicates the
    # task is still in progress ("debug", "replan") — only allow it when
    # evaluation_result is absent (simple task) or already terminal.
    _fast_path_blocked = evaluation_result in ("debug", "replan")
    if not current_plan and not next_action and execution_ok and rounds > 0 and not _fast_path_blocked:
        logger.info(
            "should_after_memory_sync: fast-path task complete "
            f"(rounds={rounds}, no plan, no pending action, evaluation_result={evaluation_result!r}) — routing to END"
        )
        return "end"

    delegations = state.get("delegations") or []
    if delegations:
        logger.info(
            f"should_after_memory_sync: {len(delegations)} delegations, routing to delegation"
        )
        return "delegation"

    logger.info("should_after_memory_sync: no delegations, routing to perception")
    return "perception"
