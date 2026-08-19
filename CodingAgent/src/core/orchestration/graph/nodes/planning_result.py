from typing import Any


def _build_resolved_plan_result(
    *,
    current_plan: list,
    current_step: int,
    plan_attempts: int,
    relevant_files: list[str],
    key_symbols: list[str],
    affected_files: list,
    execution_waves: Any,
) -> dict:
    """Assemble the standard planning-node return payload for a resolved plan."""
    return {
        "current_plan": current_plan,
        "current_step": current_step,
        "task_decomposed": True,
        "plan_dag": {"steps": current_plan},
        "execution_waves": execution_waves,
        "current_wave": 0,
        "plan_attempts": plan_attempts,
        "plan_mode_approved": None,
        "affected_files": affected_files,
        "relevant_files": relevant_files,
        "key_symbols": key_symbols,
    }
