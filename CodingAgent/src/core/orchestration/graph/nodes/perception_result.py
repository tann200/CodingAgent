import logging
from typing import Any, Mapping


async def _build_perception_result(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    content: str,
    tool_call: dict | None,
    turn_count: int,
    overflow_compaction: dict,
    model_tier_str: str | None,
    session_cost_delta: float,
    new_compacted_history: list | None,
    task_is_complex_fn: Any,
    logger: logging.Logger,
) -> dict:
    """Assemble the final perception-node state update payload."""
    current_plan = state.get("current_plan")
    current_step = state.get("current_step")
    task_decomposed = state.get("task_decomposed")
    original_task = state.get("original_task")

    if content and content.strip():
        new_messages = [{"role": "assistant", "content": content}]
    else:
        new_messages = []

    empty_response_count = int(state.get("empty_response_count") or 0)
    result = {
        "history": new_messages,
        "next_action": tool_call,
        "rounds": state.get("rounds", 0) + 1,
        "turn_count": turn_count,
        "empty_response_count": empty_response_count,
        "errors": [],
        **overflow_compaction,
    }

    if model_tier_str is not None:
        result["model_tier"] = model_tier_str

    if session_cost_delta > 0:
        prior_cost = float(state.get("session_cost_usd") or 0.0)
        result["session_cost_usd"] = round(prior_cost + session_cost_delta, 8)

    try:
        snapshot_manager = (
            getattr(orchestrator, "snapshot_manager", None) if orchestrator else None
        )
        if snapshot_manager is not None:
            snapshot_hash = await snapshot_manager.track()
            if snapshot_hash:
                prior_snaps = list(state.get("snapshots") or [])[-9:]
                prior_snaps.append(snapshot_hash)
                result["snapshots"] = prior_snaps
    except Exception:
        pass

    if current_plan is not None:
        result["current_plan"] = current_plan
    if current_step is not None:
        result["current_step"] = current_step
    if task_decomposed is not None:
        result["task_decomposed"] = task_decomposed
    if original_task is not None:
        result["original_task"] = original_task

    current_agent_mode = getattr(orchestrator, "_agent_mode", None) if orchestrator else None
    if current_agent_mode is not None:
        result["agent_mode"] = current_agent_mode

    try:
        if task_is_complex_fn is None:
            raise RuntimeError("builder unavailable")
        task_complexity = "complex" if task_is_complex_fn(state) else "simple"
        result["task_complexity"] = task_complexity
        logger.info("perception_node WF-1: task_complexity=%s", task_complexity)
    except Exception:
        pass

    if new_compacted_history is not None:
        result["_compacted_history"] = new_compacted_history
        result["_compaction_last_round"] = int(state.get("rounds") or 0)

    return result
