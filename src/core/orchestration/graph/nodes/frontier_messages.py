"""frontier_messages.py — message-shaping helpers for the frontier LLM loop.

Split out of ``frontier_loop_node.py`` (P3.1) to keep that node within its
size budget.  These two helpers are pure functions over the chat message list;
they own the legacy-text <-> native-tool-call bridging used by every frontier
turn.  Behavior is carried over verbatim from the original location; the
original module re-exports both names so the legacy test surface and any
in-module callers keep working unchanged.
"""

from __future__ import annotations

import json
from typing import Any


def _extract_content_text(response: Any) -> str:
    """Extract text content from an LLM response object."""
    try:
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices and isinstance(choices, list):
                msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        parts = []
                        for item in content:
                            if isinstance(item, str):
                                parts.append(item)
                            elif isinstance(item, dict):
                                parts.append(item.get("text", ""))
                        return "\n".join(p for p in parts if p)
        content = response.content if hasattr(response, "content") else response  # type: ignore[union-attr]
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("text", ""))
            return "\n".join(p for p in parts if p)
    except Exception:
        pass
    return ""


def _extract_tool_call_from_text(content: str) -> tuple[dict | None, str]:
    """Scan *content* for an embedded JSON tool-call block and return (tool_call_dict, remaining_text).

    Handles:
    1. Pure JSON: ``{"name": "read_file", "arguments": {...}}``
    2. JSON embedded at the end of a thinking block (e.g. Gemma returns reasoning text
       followed by a newline-separated JSON object).

    Returns ``(None, content)`` when no tool call JSON is found.
    """
    # Fast-path: entire content is a JSON tool call
    _stripped = content.strip()
    try:
        _obj = json.loads(_stripped)
        if isinstance(_obj, dict) and ("name" in _obj or "tool" in _obj):
            _tc_name = _obj.get("name") or _obj.get("tool", "")
            _tc_args = _obj.get("arguments") or _obj.get("args") or {}
            if isinstance(_tc_args, str):
                try:
                    _tc_args = json.loads(_tc_args)
                except Exception:
                    _tc_args = {}
            if _tc_name:
                return {"name": _tc_name, "arguments": _tc_args}, ""
    except Exception:
        pass

    # Slow-path: scan for a balanced JSON object containing "name" or "tool" key.
    # Walk through content right-to-left looking for '{' that starts a balanced JSON
    # object at or near the end of the string.
    def _try_parse_balanced_json_at(text: str, start: int) -> tuple[dict | None, int]:
        """Try to parse a balanced JSON object starting at text[start]. Returns (obj, end_idx) or (None, -1)."""
        if text[start] != '{':
            return None, -1
        depth = 0
        in_str = False
        escape = False
        i = start
        while i < len(text):
            ch = text[i]
            if escape:
                escape = False
            elif ch == '\\' and in_str:
                escape = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            obj = json.loads(candidate)
                            return obj, i + 1
                        except Exception:
                            return None, -1
            i += 1
        return None, -1

    # Find all '{' positions in reverse order (prefer last occurrence)
    positions = [i for i, ch in enumerate(content) if ch == '{']
    for pos in reversed(positions):
        obj, end = _try_parse_balanced_json_at(content, pos)
        if obj is not None and isinstance(obj, dict) and ("name" in obj or "tool" in obj):
            _tc_name = obj.get("name") or obj.get("tool", "")
            _tc_args = obj.get("arguments") or obj.get("args") or {}
            if isinstance(_tc_args, str):
                try:
                    _tc_args = json.loads(_tc_args)
                except Exception:
                    _tc_args = {}
            if _tc_name:
                _remaining = content[:pos].strip()
                return {"name": _tc_name, "arguments": _tc_args}, _remaining

    return None, content


def _normalize_messages_for_native_tools(messages: list[dict]) -> list[dict]:
    """Reformat message history for native tool-calling API (OpenAI format).

    When using native tool calling, the conversation history must follow the
    OpenAI protocol:
      - assistant messages that called a tool must have a ``tool_calls`` array
        (not just text content)
      - tool results must use ``role="tool"`` with ``tool_call_id``

    This function post-processes a message list that was built using the legacy
    text-based format (tool calls as JSON text in assistant content, tool results
    as ``role="user"`` with ``{"tool_execution_result": ...}`` JSON) and converts
    it into the correct native format.

    Messages that are not tool-related (plain user/assistant text) are left as-is.
    """
    normalized: list[dict] = []
    _id_counter = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        # ---- Detect assistant messages that contain a tool call as JSON text ----
        if role == "assistant" and content and not msg.get("tool_calls"):
            _tc_dict, _remaining = _extract_tool_call_from_text(content)
            if _tc_dict:
                _id_counter += 1
                _call_id = f"call_{_id_counter}"
                _native_tc = {
                    "id": _call_id,
                    "type": "function",
                    "function": {
                        "name": _tc_dict["name"],
                        "arguments": json.dumps(_tc_dict.get("arguments") or {}),
                    },
                }
                # Emit the assistant message with tool_calls (and optional thinking text)
                _asst_msg: dict = {
                    "role": "assistant",
                    "content": _remaining or "",  # use empty string, not None
                    "tool_calls": [_native_tc],
                    "_call_id": _call_id,
                }
                normalized.append(_asst_msg)
                continue

        # ---- Detect assistant messages that already have tool_calls ----
        if role == "assistant" and msg.get("tool_calls"):
            # Carry the last tool_call id for pairing with tool results
            _tc_list = msg["tool_calls"]
            _last_id = None
            if _tc_list and isinstance(_tc_list[-1], dict):
                _last_id = _tc_list[-1].get("id")
            _new_msg = dict(msg)
            if _last_id:
                _new_msg["_call_id"] = _last_id
            normalized.append(_new_msg)
            continue

        # ---- Detect user messages that are tool execution results ----
        if role == "user" and content:
            _stripped = content.strip()
            # Strip off any appended task block before trying to parse JSON
            # (append_task_message wraps the last user msg with <task> tags)
            _json_part = _stripped
            if "<task>" in _json_part:
                _json_part = _json_part[: _json_part.index("<task>")].strip()
            try:
                _obj = json.loads(_json_part)
                if isinstance(_obj, dict) and "tool_execution_result" in _obj:
                    # Find the most recent assistant message's call_id
                    _call_id = "call_0"
                    for _prev in reversed(normalized):
                        if _prev.get("_call_id"):
                            _call_id = _prev["_call_id"]
                            break
                    _result = _obj["tool_execution_result"]
                    if isinstance(_result, dict):
                        _result_text = (
                            _result.get("content")
                            or _result.get("output")
                            or json.dumps(_result)
                        )
                    else:
                        _result_text = str(_result)
                    normalized.append(
                        {
                            "role": "tool",
                            "tool_call_id": _call_id,
                            "content": _result_text,
                        }
                    )
                    continue
            except Exception:
                pass

        # ---- Pass through all other messages ----
        normalized.append(dict(msg))

    # Remove internal carry fields before returning
    for _m in normalized:
        _m.pop("_call_id", None)

    return normalized
