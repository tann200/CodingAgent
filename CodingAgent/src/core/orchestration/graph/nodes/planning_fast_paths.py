

def _build_planning_error_result(
    *,
    current_plan: list,
    current_step: int,
    plan_attempts: int,
    errors: list[str],
) -> dict:
    """Assemble the standard planning-node error payload."""
    return {
        "current_plan": current_plan,
        "current_step": current_step,
        "plan_attempts": plan_attempts,
        "plan_mode_approved": None,
        "errors": errors,
    }


def _build_resumed_plan_result(
    *,
    loaded_plan: list,
    loaded_step: int,
    plan_attempts: int,
) -> dict:
    """Assemble the resumed-plan payload."""
    return {
        "current_plan": loaded_plan,
        "current_step": loaded_step,
        "task_decomposed": True,
        "plan_resumed": True,
        "plan_attempts": plan_attempts,
        "plan_mode_approved": None,
    }


def _build_existing_plan_result(
    *,
    current_plan: list,
    current_step: int,
    step_description: str,
    plan_attempts: int,
) -> dict:
    """Assemble the early-return payload for an already decomposed plan."""
    return {
        "current_plan": current_plan,
        "current_step": current_step,
        "step_description": step_description,
        "task_decomposed": True,
        "plan_attempts": plan_attempts,
        "plan_mode_approved": None,
    }


def _build_simple_next_action_plan_result(
    *,
    current_plan: list,
    current_step: int,
    plan_attempts: int,
) -> dict:
    """Assemble the trivial one-step plan payload from an existing next_action."""
    return {
        "current_plan": current_plan,
        "current_step": current_step,
        "plan_attempts": plan_attempts,
        "plan_mode_approved": None,
    }


def _build_planning_early_response_result(
    *,
    current_plan: list,
    current_step: int,
    plan_attempts: int,
    early_resp: dict,
) -> dict:
    """Assemble the planning-node payload when LLM helper returns early."""
    result = {
        "current_plan": current_plan,
        "current_step": current_step,
        "plan_attempts": plan_attempts,
        "plan_mode_approved": None,
        "errors": early_resp.get("errors") or [],
    }
    if early_resp.get("next_action"):
        result["next_action"] = early_resp.get("next_action")
    return result
