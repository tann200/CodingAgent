import logging
from typing import Dict, Any, Literal

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


async def evaluation_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Evaluation Node: Post-verification review to decide if task goal is fully met.
    Reviews overall state including verification results, plan completion, and errors.
    Routes to: end (task complete), memory_sync (save and end), or replan (retry).
    Uses the 'reviewer' role for quality assurance.
    """
    logger.info("=== evaluation_node START ===")

    brain = get_agent_brain_manager()
    reviewer_role = brain.get_role("reviewer") or "You are a QA reviewer."

    verification_result = state.get("verification_result") or {}
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    errors = state.get("errors") or []
    rounds = state.get("rounds", 0)

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

    # Determine outcome
    if verification_passed and plan_completed and not critical_errors:
        logger.info("evaluation_node: TASK COMPLETE - all checks passed")
        return {
            "evaluation_result": "complete",
            "next_action": None,
        }
    elif not verification_passed:
        logger.info(f"evaluation_node: VERIFICATION FAILED - {failure_reasons}")
        # Check if we should replan or end
        if rounds < 10 and len(current_plan) > 0:
            return {
                "evaluation_result": "replan",
                "replan_required": f"Verification failed: {'; '.join(failure_reasons)}",
                "action_failed": True,
            }
        else:
            logger.warning(
                "evaluation_node: max rounds reached, ending despite failures"
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
        # Partial completion - check if we should continue
        logger.info(
            "evaluation_node: PARTIAL COMPLETION - checking if more work needed"
        )
        if current_plan and current_step < len(current_plan):
            return {
                "evaluation_result": "replan",
                "replan_required": "Additional steps remain in plan",
                "action_failed": False,
            }
        else:
            return {
                "evaluation_result": "complete",
                "next_action": None,
            }
