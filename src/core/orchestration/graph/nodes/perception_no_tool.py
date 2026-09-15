from src.core.messaging.event_types import PerceptionCorrectivePrompt
import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


# P1-D: Graduated corrective prompts helper.
# Selects a corrective prompt variant based on the number of consecutive
# empty/no-tool responses (attempt) and model tier.
def _select_corrective_prompt(
    attempt: int = 1,
    model_tier: str | None = None,
    truncated_yaml: bool = False,
) -> str:
    try:
        att = int(attempt or 1)
    except Exception:
        att = 1
    # Graduated prompts: gentle -> specific -> critical
    prompts = [
        (
            "\n\n<system_reminder>\n"
            "Please provide a valid YAML tool call for your next action.\n"
            "Use this format:\n"
            "```yaml\n"
            "name: tool_name\n"
            "arguments:\n"
            "  arg: value\n"
            "```\n"
            "Avoid empty responses or thinking-only blocks.\n"
            "If you cannot determine the next action, you may use the 'respond' tool.\n"
            "</system_reminder>\n"
        ),
        (
            "\n\n<system_reminder>\n"
            "Please output a valid YAML tool call block now. No analysis or preamble.\n"
            "```yaml\n"
            "name: tool_name\n"
            "arguments:\n"
            "  key: value\n"
            "```\n"
            "</system_reminder>\n"
        ),
        (
            "\n\n<system_reminder>\n"
            "Important: Please provide a valid YAML tool call block.\n"
            "Format:\n"
            "```yaml\n"
            "name: tool_name\n"
            "arguments:\n"
            "  key: value\n"
            "```\n"
            "Avoid thinking-only responses or empty outputs.\n"
            "</system_reminder>\n"
        ),
    ]
    idx = max(0, min(att - 1, len(prompts) - 1))
    tier = (model_tier or "").lower()
    if truncated_yaml:
        return (
            "\n\n<system_reminder>\n"
            "Your previous YAML tool block may have been cut off or malformed. "
            "Please resend a complete YAML tool call.\n"
            "```yaml\n"
            "name: tool_name\n"
            "arguments:\n"
            "  key: value\n"
            "```\n"
            "</system_reminder>\n"
        )
    if tier == "small" and att >= 2:
        return prompts[1]
    return prompts[idx]


def _handle_no_tool_or_empty_response(
    content: str,
    content_stripped: str,
    thinking_only: bool,
    state: Mapping[str, Any],
    orchestrator: Any,
    _model_tier_str: str | None,
    *,
    _is_truncated_yaml: bool = False,
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

    corrective_prompt = _select_corrective_prompt(
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
                orchestrator.event_bus.publish_typed(
                    PerceptionCorrectivePrompt(
                        session_id=state.get("session_id", ""),
                        attempt=int(empty_response_count),
                        reason="no_tool",
                        model_tier=_model_tier_str or "",
                        truncated_yaml=str(_is_truncated_yaml),
                    )
                )
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
