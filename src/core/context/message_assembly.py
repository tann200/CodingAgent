from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Mapping, Sequence


def sanitize_conversation_messages(
    *,
    conversation: Sequence[Mapping[str, object]],
    sanitize_text: Callable[[str], str],
) -> List[Dict[str, Any]]:
    """Sanitize conversation messages while preserving paired structured tool turns.

    Keeps ``user`` and ``assistant`` turns (including ``tool_calls``,
    ``thinking_blocks``, and ``reasoning_content`` on assistant turns) and keeps
    ``role == 'tool'`` turns whose ``tool_call_id`` matches a preceding
    assistant ``tool_calls`` entry. Orphan ``tool`` messages without a matching
    preceding ``assistant.tool_calls`` are dropped to prevent provider HTTP 400s.
    """
    sanitized: List[Dict[str, Any]] = []
    valid_tool_call_ids: set[str] = set()

    for message in conversation:
        role = str(message.get("role") or "")
        if role == "user":
            sanitized.append(
                {
                    "role": "user",
                    "content": sanitize_text(str(message.get("content", ""))),
                }
            )
        elif role == "assistant":
            entry: Dict[str, Any] = {
                "role": "assistant",
                "content": sanitize_text(str(message.get("content", "") or "")),
            }
            raw_tcs = message.get("tool_calls")
            if isinstance(raw_tcs, list) and raw_tcs:
                clean_tcs: List[Dict[str, Any]] = []
                for tc in raw_tcs:
                    if isinstance(tc, Mapping):
                        tc_dict = dict(tc)
                        tc_id = str(tc_dict.get("id") or "")
                        if tc_id:
                            valid_tool_call_ids.add(tc_id)
                        clean_tcs.append(tc_dict)
                if clean_tcs:
                    entry["tool_calls"] = clean_tcs
            if isinstance(message.get("thinking_blocks"), list) and message.get(
                "thinking_blocks"
            ):
                entry["thinking_blocks"] = list(message["thinking_blocks"])  # type: ignore[arg-type]
            if message.get("reasoning_content"):
                entry["reasoning_content"] = str(message["reasoning_content"])
            sanitized.append(entry)
        elif role == "tool":
            tc_id = str(message.get("tool_call_id") or "")
            if tc_id and tc_id in valid_tool_call_ids:
                tool_entry: Dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": sanitize_text(str(message.get("content", "") or "")),
                }
                if message.get("name"):
                    tool_entry["name"] = str(message["name"])
                sanitized.append(tool_entry)

    return sanitized


def _group_for_quota_truncation(
    conversation: Sequence[Mapping[str, Any]],
) -> List[List[Dict[str, Any]]]:
    """Group ``assistant(tool_calls)`` with its immediately following ``tool`` results."""
    groups: List[List[Dict[str, Any]]] = []
    i = 0
    n = len(conversation)
    while i < n:
        msg = dict(conversation[i])
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            grp = [msg]
            j = i + 1
            while j < n and conversation[j].get("role") == "tool":
                grp.append(dict(conversation[j]))
                j += 1
            groups.append(grp)
            i = j
        else:
            groups.append([msg])
            i += 1
    return groups


def truncate_conversation_to_quota(
    *,
    conversation: Sequence[Mapping[str, Any]],
    conversation_quota: int,
    token_estimator: Callable[[str], int],
) -> List[Dict[str, Any]]:
    truncated: List[Dict[str, Any]] = []
    if conversation_quota <= 0 or not conversation:
        return truncated

    groups = _group_for_quota_truncation(conversation)
    total_tokens = 0
    for grp in reversed(groups):
        grp_tokens = sum(
            token_estimator(json.dumps(m, default=str)) for m in grp
        )
        if total_tokens + grp_tokens <= conversation_quota:
            truncated = grp + truncated
            total_tokens += grp_tokens
        else:
            break
    return truncated


def append_task_message(
    *,
    built_messages: List[Dict[str, Any]],
    truncated_conversation: Sequence[Mapping[str, Any]],
    task_prompt_content: str,
) -> List[Dict[str, Any]]:
    messages = list(built_messages)
    messages.extend(dict(message) for message in truncated_conversation)

    if truncated_conversation and truncated_conversation[0].get("role") == "assistant":
        messages.insert(1, {"role": "user", "content": task_prompt_content})
        return messages

    # Structured native tool history ending with role == 'tool': never append a
    # duplicate <task> user prompt after a tool result (preserves KV prefix!).
    if messages and messages[-1].get("role") == "tool":
        has_user = any(m.get("role") == "user" for m in messages)
        if not has_user:
            messages.insert(1, {"role": "user", "content": task_prompt_content})
        return messages

    if not messages or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": task_prompt_content})
        return messages

    last_message = messages[-1]
    if "<task>" not in str(last_message.get("content", "")):
        last_message["content"] = (
            f"<task>\n{last_message['content']}\n</task>\n\n"
            "Execute the next action using JSON function calling format."
        )
    return messages
