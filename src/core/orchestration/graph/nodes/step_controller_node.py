import logging
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState

logger = logging.getLogger(__name__)


async def step_controller_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Step Controller: Enforces single-step execution from the plan.
    Validates that the current step matches the planned action.
    """
    logger.info("=== step_controller_node START ===")

    current_plan = state.get("current_plan")
    if not isinstance(current_plan, list):
        current_plan = []
    current_step = state.get("current_step")
    if not isinstance(current_step, int):
        current_step = 0
    step_controller_enabled = bool(state.get("step_controller_enabled", True))

    if not step_controller_enabled or not current_plan:
        logger.info("step_controller_node: disabled or no plan, passing through")
        return {}

    plan_len = len(current_plan)
    if current_step >= plan_len:
        logger.info(
            f"step_controller_node: step {current_step} >= plan length {plan_len}"
        )
        return {"next_action": None}

    current_step_data = current_plan[current_step]
    step_description = str(current_step_data.get("description", ""))
    planned_action = current_step_data.get("action")

    # H2: Track per-step retry counts so should_after_step_controller can cap retries.
    step_retry_counts: dict = dict(state.get("step_retry_counts") or {})
    step_key = str(current_step)
    last_result = state.get("last_result")
    step_failed = bool(
        last_result
        and isinstance(last_result, dict)
        and not (last_result.get("ok") or last_result.get("status") == "ok")
    )
    if step_failed:
        step_retry_counts[step_key] = int(step_retry_counts.get(step_key, 0)) + 1
        logger.info(
            f"step_controller_node: step {current_step + 1} retry #{step_retry_counts[step_key]}"
        )

    logger.info(
        f"step_controller_node: enforcing step {current_step + 1}/{plan_len}: {step_description}"
    )

    return {
        "step_description": step_description,
        "planned_action": planned_action,
        "step_retry_counts": step_retry_counts,
    }
