from __future__ import annotations

import json
from typing import Callable, Dict, List, Mapping, Sequence


def sanitize_conversation_messages(
    *,
    conversation: Sequence[Mapping[str, object]],
    sanitize_text: Callable[[str], str],
) -> List[Dict[str, str]]:
    return [
        {
            "role": str(message.get("role")),
            "content": sanitize_text(str(message.get("content", ""))),
        }
        for message in conversation
        if message.get("role") in ["user", "assistant"]
    ]


def truncate_conversation_to_quota(
    *,
    conversation: Sequence[Mapping[str, str]],
    conversation_quota: int,
    token_estimator: Callable[[str], int],
) -> List[Dict[str, str]]:
    truncated: List[Dict[str, str]] = []
    if conversation_quota <= 0 or not conversation:
        return truncated

    total_tokens = 0
    for message in reversed(conversation):
        message_json = json.dumps(message)
        message_tokens = token_estimator(message_json)
        if total_tokens + message_tokens <= conversation_quota:
            truncated.insert(0, dict(message))
            total_tokens += message_tokens
        else:
            break
    return truncated


def append_task_message(
    *,
    built_messages: List[Dict[str, str]],
    truncated_conversation: Sequence[Mapping[str, str]],
    task_prompt_content: str,
) -> List[Dict[str, str]]:
    messages = list(built_messages)
    messages.extend(dict(message) for message in truncated_conversation)

    if truncated_conversation and truncated_conversation[0].get("role") == "assistant":
        messages.insert(1, {"role": "user", "content": task_prompt_content})
        return messages

    if not messages or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": task_prompt_content})
        return messages

    last_message = messages[-1]
    if "<task>" not in last_message.get("content", ""):
        last_message["content"] = (
            f"<task>\n{last_message['content']}\n</task>\n\n"
            "Execute the next action using JSON function calling format."
        )
    return messages
