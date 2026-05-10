import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def _handle_no_tool_or_empty_response(
    content: str,
    content_stripped: str,
    thinking_only: bool,
    state: Mapping[str, Any],
    orchestrator: Any,
    _model_tier_str: str | None,
    *,
    _is_truncated_yaml: bool = False,
    select_corrective_prompt: Any,
) -> dict | None:
    """Encapsulate corrective-prompt retry logic when no tool was parsed."""
    if not (content_stripped or thinking_only):
        return None

    empty_response_count = int(state.get("empty_response_count") or 0) + 1
    logger.info(
        f"perception_node: No tool call extracted (count: {empty_response_count})"
    )

    tier = (_model_tier_str or "").lower()
    if tier == "small":
        max_corrective = 2
    elif tier == "medium":
        max_corrective = 3
    else:
        max_corrective = 4

    if empty_response_count >= max_corrective:
        logger.error(
            f"perception_node: {max_corrective} consecutive failed tool extractions (tier={tier}) - breaking loop"
        )
        return {
            "history": [{"role": "assistant", "content": content or ""}],
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "last_result": {
                "ok": False,
                "error": f"Infinite loop detected: model failed to generate valid tool calls {max_corrective} times",
            },
            "errors": ["infinite_loop_no_tool"],
            "empty_response_count": 0,
        }

    corrective_prompt = select_corrective_prompt(
        attempt=empty_response_count,
        model_tier=_model_tier_str,
        truncated_yaml=_is_truncated_yaml,
    )
    new_messages = [
        {"role": "assistant", "content": content or ""},
        {
            "role": "user",
            "content": corrective_prompt + "\n\nProvide a valid JSON function call now.",
        },
    ]

    try:
        if orchestrator and hasattr(orchestrator, "event_bus"):
            event = {
                "session_id": state.get("session_id"),
                "attempt": empty_response_count,
                "reason": "no_tool",
                "model_tier": _model_tier_str,
                "truncated_yaml": _is_truncated_yaml,
            }
            try:
                orchestrator.event_bus.publish("perception.corrective_prompt", event)
            except Exception:
                publish = getattr(orchestrator.event_bus, "publish", None)
                if callable(publish):
                    publish("perception.corrective_prompt", event)
    except Exception:
        pass

    return {
        "history": new_messages,
        "next_action": None,
        "rounds": state.get("rounds", 0) + 1,
        "empty_response_count": empty_response_count,
    }


def _maybe_return_content_after_no_tool_retry(
    content_no_thinking: str,
    state: Mapping[str, Any],
    rounds_now: int,
    turn_count: int,
    model_tier_str: str | None,
) -> dict | None:
    """Return a final assistant message when a retry yields plain content."""
    current_empty_response_count = int(state.get("empty_response_count") or 0)
    if not (content_no_thinking.strip() and current_empty_response_count >= 1):
        return None

    content_lower = content_no_thinking.lower().strip()
    if any(
        phrase in content_lower
        for phrase in [
            "thinking process",
            "analyze the request",
            "let me think",
            "i need to think",
            "first,",
            "second,",
            "third,",
            "step 1",
            "step 2",
            "step 3",
        ]
    ):
        return None

    return {
        "history": [
            {"role": "assistant", "content": content_no_thinking.strip()}
        ],
        "next_action": None,
        "rounds": rounds_now + 1,
        "turn_count": turn_count,
        "empty_response_count": 0,
        **({"model_tier": model_tier_str} if model_tier_str else {}),
    }
