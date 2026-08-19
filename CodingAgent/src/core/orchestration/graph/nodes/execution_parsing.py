"""execution_parsing.py — LLM response parsing helpers.

Extracted from execution_helpers.py (P3-4) for improved modularity.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple


def extract_tool_call_from_response(
    response: Any,
    *,
    parse_tool_block: Callable[[str], Optional[dict]],
    logger: Any = None,
) -> Tuple[str, Optional[List[Any]], Optional[dict]]:
    content = ""
    tool_calls = None

    if isinstance(response, dict):
        choices = response.get("choices")
        if choices and len(choices) > 0:
            choice_message = (
                choices[0].get("message") if isinstance(choices[0], dict) else None
            )
            if isinstance(choice_message, dict):
                content = choice_message.get("content") or ""
                tool_calls = choice_message.get("tool_calls")
        elif response.get("message"):
            message = response.get("message")
            if isinstance(message, dict):
                content = message.get("content", "")
                tool_calls = message.get("tool_calls")

    tool_call = None
    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
        first_tool_call = tool_calls[0]
        if isinstance(first_tool_call, dict):
            func = first_tool_call.get("function")
            if func:
                name = func.get("name")
                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if name:
                    tool_call = {"name": name, "arguments": args or {}}
                    if logger is not None:
                        try:
                            logger.info(
                                "execution_node: native function call: %s",
                                name,
                            )
                        except Exception:
                            pass
    if not tool_call:
        tool_call = parse_tool_block(content)

    return content, tool_calls, tool_call


def build_no_action_result(
    *,
    content: str,
    action: Any,
    state: Mapping[str, Any],
    current_plan: Optional[List[Dict[str, Any]]],
    current_step: int,
    original_task: Optional[str],
    inc_step_retry: Callable[[Mapping[str, Any], int], dict],
) -> Optional[Dict[str, Any]]:
    if action:
        return None

    content_lower = (content or "").lower()

    if content is not None and content.strip() == "" and not action:
        return {
            "last_result": {
                "ok": False,
                "error": "empty_model_response: model produced no content. "
                "The conversation history may be too long for the context window. "
                "Try breaking the task into smaller steps.",
            },
            "step_retry_counts": inc_step_retry(state, current_step),
            "history": [],
        }

    if re.search(r"status\s*:\s*complete", content_lower):
        updated_plan = [dict(step) for step in current_plan] if current_plan else []
        if updated_plan and current_step < len(updated_plan):
            updated_plan[current_step]["completed"] = True
            next_step = current_step + 1
            plan_advance: Dict[str, Any] = {
                "current_step": next_step,
                "current_plan": updated_plan,
            }
            if next_step < len(updated_plan):
                plan_advance["task"] = updated_plan[next_step].get("description", "")
            else:
                plan_advance["task"] = original_task or "Task complete"
        else:
            plan_advance = {}

        history_entry = [{"role": "assistant", "content": content}] if content else []
        return {
            "last_result": {
                "ok": True,
                "status": "ok",
                "completed_without_tool": True,
            },
            "history": history_entry,
            **plan_advance,
        }

    return {
        "last_result": {"ok": False, "error": "format_error: no tool call emitted"},
        "step_retry_counts": inc_step_retry(state, current_step),
    }
