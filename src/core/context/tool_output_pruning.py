"""Helpers for pruning stale tool outputs from prompt conversations."""

from __future__ import annotations

import json
from typing import Dict, List, Optional


def prune_stale_tool_outputs(
    messages: List[Dict],
    *,
    current_step_hint: Optional[str] = None,
    stale_after_turns: int = 3,
) -> List[Dict]:
    """Replace stale tool-result messages with compact stubs."""
    if not messages:
        return messages

    tool_result_indices: List[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if "tool_execution_result" in str(content):
                tool_result_indices.append(i)

    recent_indices: set[int] = set(tool_result_indices[-stale_after_turns:])
    pruned: List[Dict] = []

    for i, msg in enumerate(messages):
        if (
            msg.get("role") == "user"
            and i not in recent_indices
            and "tool_execution_result" in str(msg.get("content", ""))
        ):
            try:
                content_str = msg.get("content", "")
                data = json.loads(content_str) if isinstance(content_str, str) else {}
                res = data.get("tool_execution_result", {})
                tool_name = res.get("tool_name") or res.get("name") or "tool"
                is_ok = bool(res.get("ok") or res.get("status") == "ok")
                status = "ok" if is_ok else "error"

                if current_step_hint and current_step_hint.lower() in str(content_str).lower():
                    pruned.append(msg)
                    continue

                stub = json.dumps(
                    {
                        "tool_execution_result": {
                            "tool_name": tool_name,
                            "status": status,
                            "_pruned": True,
                            "_note": f"Full output pruned (stale - >{stale_after_turns} turns ago). Use read_file to re-fetch if needed.",
                        }
                    }
                )
                pruned.append({"role": "user", "content": stub})
            except Exception:
                pruned.append(msg)
        else:
            pruned.append(msg)

    return pruned
