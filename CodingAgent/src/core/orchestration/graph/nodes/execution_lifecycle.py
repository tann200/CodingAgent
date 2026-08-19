"""execution_lifecycle.py — Retry, orchestrator resolution, cancellation, and logging.

Extracted from execution_helpers.py (P3-4) for improved modularity.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from src.core.orchestration.prompt_injection_guard import sanitize_result_dict


def increment_step_retry_count(state: Mapping[str, Any], current_step: int) -> dict:
    """Return step retry counts with the current step incremented.

    Keys are normalized to strings so persisted state and routing logic use one
    canonical representation. Legacy int-keyed state is folded into the same
    string key during normalization.
    """
    raw = state.get("step_retry_counts") or {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        try:
            counts[str(int(key))] = int(value)
        except (ValueError, TypeError):
            try:
                counts[str(key)] = int(value)
            except (ValueError, TypeError):
                continue
    step_key = str(current_step)
    counts[step_key] = counts.get(step_key, 0) + 1
    return counts


def resolve_execution_orchestrator(
    *,
    state: Mapping[str, Any],
    config: Any,
    resolve_orchestrator_fn: Callable[[Mapping[str, Any], Any], Any],
    logger: Any,
) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """Resolve the execution orchestrator and normalize subagent/config errors."""
    try:
        orchestrator = resolve_orchestrator_fn(state, config)
        if orchestrator is not None:
            return orchestrator, None

        config_has_orchestrator_field = False
        if config and isinstance(config, dict):
            cfg = config.get("configurable") or config
            if cfg is not None and "orchestrator" in cfg:
                config_has_orchestrator_field = True

        if config_has_orchestrator_field:
            logger.info("execution_node: subagent mode (orchestrator=None in config)")
            return None, None

        logger.error("execution_node: orchestrator is None")
        return None, {
            "last_result": None,
            "errors": ["orchestrator not found"],
        }
    except Exception as exc:
        logger.error("execution_node: failed to get orchestrator: %s", exc)
        return None, {
            "last_result": None,
            "errors": [f"config error: {exc}"],
        }


def maybe_build_execution_cancellation_result(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    logger: Any,
) -> Optional[Dict[str, Any]]:
    """Return the standard canceled payload when a cancel event is set."""
    cancel_event = state.get("cancel_event")
    if not cancel_event:
        cancel_event = getattr(orchestrator, "cancel_event", None)
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("execution_node: Task canceled by user")
        return {
            "last_result": {"ok": False, "error": "Task canceled by user"},
            "errors": ["canceled"],
            "next_action": None,
        }
    return None


def select_execution_action(*, state: Mapping[str, Any], logger: Any) -> Any:
    """Select the current execution action, preferring planned_action over next_action."""
    try:
        return state.get("planned_action") or state.get("next_action")
    except Exception as exc:
        logger.error("execution_node: failed to get next_action: %s", exc)
        return None


def maybe_begin_step_transaction(*, orchestrator: Any, logger: Any) -> None:
    """Begin a step transaction when supported, logging non-fatal failures."""
    try:
        if orchestrator and hasattr(orchestrator, "begin_step_transaction"):
            orchestrator.begin_step_transaction()
            logger.debug("execution_node: step transaction started")
    except Exception as exc:
        logger.debug(
            "execution_node: step transaction init failed (non-fatal): %s",
            exc,
        )


def log_wave_execution_start(
    *,
    execution_waves: Optional[Sequence[Any]],
    current_wave: int,
    logger: Any,
) -> None:
    """Log the current wave size when execution is operating in wave mode.

    P3-5 / OE-3 NOTE: DAG waves are tracked for advancement detection but
    execution is **sequential** (one tool call per execution_node invocation).
    True parallel wave execution would require asyncio.gather across multiple
    simultaneous invocations or LangGraph Send() branches — a high-complexity
    change deferred to a future phase.  The wave infrastructure is still
    valuable: it groups steps logically, enables wave-level progress tracking,
    and provides a clear upgrade path to parallelism.
    """
    if execution_waves and current_wave < len(execution_waves):
        wave_steps = execution_waves[current_wave]
        logger.info(
            "Wave execution: wave %d/%d with %d steps (sequential)",
            current_wave + 1,
            len(execution_waves),
            len(wave_steps),
        )


def log_plan_and_wave_advancement(
    *,
    plan_advance: Mapping[str, Any],
    wave_advance: Mapping[str, Any],
    current_plan: Optional[Sequence[Any]],
    current_step: int,
    execution_waves: Optional[Sequence[Any]],
    current_wave: int,
    logger: Any,
) -> None:
    """Log human-readable advancement for plan steps and execution waves."""
    if plan_advance:
        if current_plan and plan_advance.get("current_step", 0) < len(
            plan_advance.get("current_plan") or []
        ):
            logger.info(
                "Step %d complete, advancing to step %d",
                current_step + 1,
                plan_advance["current_step"] + 1,
            )
        else:
            logger.info("All plan steps completed")

    if wave_advance:
        if wave_advance.get("current_wave", 0) < len(execution_waves or []):
            logger.info(
                "Wave %d complete, advancing to wave %d",
                current_wave + 1,
                wave_advance["current_wave"] + 1,
            )
        else:
            logger.info("All waves completed")


def log_plan_step_execution(
    *,
    current_plan: Optional[Sequence[Mapping[str, Any]]],
    current_step: int,
    task_decomposed: bool,
    original_task: Optional[str],
    logger: Any,
) -> None:
    """Log the current plan step when executing a decomposed task."""
    if not current_plan or current_step >= len(current_plan):
        return
    if not task_decomposed or not original_task:
        return

    current_step_desc = current_plan[current_step].get("description", "")
    logger.info(
        "Plan execution: step %d/%d - %s",
        current_step + 1,
        len(current_plan),
        current_step_desc,
    )


def sync_tool_result_to_ui(
    *, orchestrator: Any, result: Mapping[str, Any], logger: Any,
    tool_name: str = "",
) -> None:
    """Best-effort append of the latest tool result into UI-visible history."""
    if not orchestrator or not hasattr(orchestrator, "msg_mgr"):
        return
    try:
        safe_result = sanitize_result_dict(dict(result), tool_name=tool_name)
        orchestrator.msg_mgr.append(
            "user", json.dumps({"tool_execution_result": safe_result})
        )
    except Exception as exc:
        logger.debug("UI sync failed: %s", exc)


def log_no_action_outcome(*, content: str, logger: Any, regex_module: Any = None) -> None:
    """Log the reason a no-action result was returned."""
    import re as _re
    regex_module = regex_module or _re
    if content is not None and content.strip() == "":
        logger.warning(
            "execution_node: model produced empty content with no tool call — "
            "context window may be full or model failed to generate output"
        )
    elif regex_module.search(r"status\s*:\s*complete", (content or "").lower()):
        logger.info(
            "execution_node: model declared STATUS: complete with no tool call — "
            "treating as successful step completion"
        )


def sync_execution_state_to_orchestrator(
    *, state: Mapping[str, Any], orchestrator: Any
) -> None:
    """Propagate execution-scoped state needed by downstream tool enforcement."""
    if not orchestrator:
        return
    orchestrator._plan_mode_approved = state.get("plan_mode_approved")
    orchestrator._affected_files = list(state.get("affected_files") or [])
