import logging
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState

logger = logging.getLogger(__name__)


async def evaluation_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Evaluation Node: Post-verification review to decide if task goal is fully met.
    Reviews overall state including verification results, plan completion, and errors.
    Routes to: end (task complete), memory_sync (save and end), or replan (retry).
    Uses the 'reviewer' role for quality assurance.
    """
    logger.info("=== evaluation_node START ===")


    verification_result = state.get("verification_result") or {}
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    errors = state.get("errors") or []

    # Check verification status
    tests = verification_result.get("tests", {})
    linter = verification_result.get("linter", {})
    syntax = verification_result.get("syntax", {})

    verification_passed = True
    failure_reasons = []

    if tests.get("status") == "fail":
        verification_passed = False
        failure_reasons.append(f"Tests failed: {tests.get('message', 'Unknown error')}")
    if linter.get("status") == "fail":
        verification_passed = False
        failure_reasons.append(
            f"Linter failed: {linter.get('message', 'Unknown error')}"
        )
    if syntax.get("status") == "fail":
        verification_passed = False
        failure_reasons.append(
            f"Syntax errors: {syntax.get('message', 'Unknown error')}"
        )

    # Check plan completion
    plan_completed = (
        not current_plan
        or current_step >= len(current_plan)
        or all(step.get("completed", False) for step in current_plan)
    )

    # Check for critical errors
    critical_errors = [
        e for e in errors if e not in ["canceled", "orchestrator not found"]
    ]

    # Read debug_attempts to gate debug routing — this prevents an unbounded loop.
    # Previously evaluation routed to "replan" on verification failure, which sent to
    # step_controller → execution → verification → evaluation (rounds never incremented
    # in this path, so the rounds < 10 guard never fired → infinite loop).
    # Now: route to "debug" for verification failures (bounded by debug_attempts).
    debug_attempts = int(state.get("debug_attempts") or 0)
    max_debug_attempts = int(state.get("max_debug_attempts") or 3)

    # Determine outcome
    if verification_passed and plan_completed and not critical_errors:
        logger.info("evaluation_node: TASK COMPLETE - all checks passed")
        return {
            "evaluation_result": "complete",
            "next_action": None,
        }
    elif not verification_passed:
        logger.info(f"evaluation_node: VERIFICATION FAILED - {failure_reasons}")
        # Route to debug if we have debug attempts remaining — this is the bounded fix path.
        # debug_node generates a fix → execution → verification → evaluation cycle,
        # but is capped by debug_attempts so it cannot loop indefinitely.
        if debug_attempts < max_debug_attempts:
            logger.info(
                f"evaluation_node: routing to debug (attempt {debug_attempts + 1}/{max_debug_attempts})"
            )
            return {
                "evaluation_result": "debug",
            }
        else:
            logger.warning(
                f"evaluation_node: max debug attempts ({max_debug_attempts}) reached, ending"
            )
            return {
                "evaluation_result": "end",
                "next_action": None,
            }
    elif critical_errors:
        logger.info(f"evaluation_node: CRITICAL ERRORS - {critical_errors}")
        return {
            "evaluation_result": "end",
            "next_action": None,
            "errors": critical_errors,
        }
    else:
        # Partial completion — plan has remaining steps (shouldn't normally happen since
        # execution only reaches verification after the last step, but guard defensively).
        logger.info(
            "evaluation_node: PARTIAL COMPLETION - checking if more work needed"
        )
        if current_plan and current_step < len(current_plan):
            # There are pending steps: re-enter execution via step_controller.
            # This is bounded because plan steps are finite; as steps complete,
            # current_step advances until current_step >= len(current_plan).
            # W5 fix: do NOT set replan_required here — that field is consumed by
            # should_after_execution_with_replan and would route subsequent execution
            # steps to replan_node (splitting) instead of step_controller (advancing).
            # Clear it explicitly to prevent stale value from a prior F13 trigger.
            return {
                "evaluation_result": "replan",
                "replan_required": None,
                "action_failed": False,
            }
        else:
            return {
                "evaluation_result": "complete",
                "next_action": None,
            }
