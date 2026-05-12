import json
import logging
from typing import Any, Mapping

from src.core.orchestration.tool_parser import parse_tool_block

logger = logging.getLogger(__name__)


def _extract_message_obj(resp: Any) -> dict:
    """Return the normalized message object from an adapter response."""
    try:
        if isinstance(resp, dict):
            choices = resp.get("choices")
            if choices and len(choices) > 0:
                if isinstance(choices[0], dict):
                    choice = choices[0]
                    message = choice.get("message", {})
                    if isinstance(message, dict) and choice.get("tool_calls") and not message.get("tool_calls"):
                        return {**message, "tool_calls": choice.get("tool_calls")}
                    return message if isinstance(message, dict) else {}
    except Exception:
        pass
    return {}


def _parse_native_tool_call_from_resp(resp: Any) -> dict | None:
    """Parse native provider tool_calls from a response."""
    try:
        message_obj = _extract_message_obj(resp)
        native_tool_calls = message_obj.get("tool_calls")
        if (
            native_tool_calls
            and isinstance(native_tool_calls, list)
            and len(native_tool_calls) > 0
        ):
            tool_call = native_tool_calls[0]
            if isinstance(tool_call, dict):
                function = tool_call.get("function")
                if function:
                    name = function.get("name")
                    args = function.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if name:
                        logger.info(f"perception_node: native function call: {name}")
                        return {"name": name, "arguments": args or {}}
    except Exception:
        pass
    return None


def _detect_prompt_injection(tool_call: dict | None, state: Mapping[str, Any]) -> bool:
    """Detect whether a parsed tool call mirrors a prior user message."""
    if not tool_call:
        return False
    tool_name_extracted = tool_call.get("name", "")
    if not tool_name_extracted:
        return False

    user_messages = [
        message.get("content", "")
        for message in (state.get("history") or [])
        if message.get("role") == "user"
    ]
    name_pattern = f"name: {tool_name_extracted}"
    tool_args = tool_call.get("arguments") or {}
    arg_keys = list(tool_args.keys())[:3]
    for user_message in user_messages:
        if not user_message or name_pattern not in user_message:
            continue
        if arg_keys:
            if any(f"{key}:" in user_message for key in arg_keys):
                return True
        elif "arguments:" in user_message:
            return True
    return False


def _parse_tool_call_and_flags(
    resp: Any, content: str, state: Mapping[str, Any]
) -> tuple[dict | None, str, bool, str]:
    """Parse tool call from response/content and compute helper flags."""
    content_stripped = content.strip() if content else ""

    try:
        from src.core.inference.thinking_utils import strip_thinking as _strip_thinking

        content_no_thinking = _strip_thinking(content_stripped)
    except Exception:
        content_no_thinking = content_stripped

    thinking_only = not content_no_thinking

    try:
        prior_history = state.get("history") or []
        if isinstance(prior_history, list) and prior_history:
            last_msg = prior_history[-1]
            if last_msg.get("role") == "tool":
                logger.info("perception_node: last message was a tool result")
    except Exception:
        pass

    tool_call = None
    try:
        tool_call = _parse_native_tool_call_from_resp(resp)

        if (
            not tool_call
            and content
            and "tool_execution_result" not in content
            and '"tool_execution_result"' not in content
        ):
            tool_call = parse_tool_block(content)
        elif not tool_call:
            logger.info(
                "perception_node: skipping parse_tool_block because content contains tool_execution_result"
            )

        if tool_call is not None and _detect_prompt_injection(tool_call, state):
            tool_name_extracted = tool_call.get("name", "")
            logger.warning(
                f"perception_node: F8 injection guard — tool call '{tool_name_extracted}' "
                "matches a user-role message (name + args); rejecting to prevent prompt injection"
            )
            tool_call = None
    except Exception:
        tool_call = None

    return tool_call, content_stripped, thinking_only, content_no_thinking
