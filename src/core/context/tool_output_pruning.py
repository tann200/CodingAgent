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

    native_tool_indices: List[int] = []
    legacy_tool_indices: List[int] = []
    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content", "")
        has_legacy_marker = "tool_execution_result" in str(content)
        if has_legacy_marker:
            legacy_tool_indices.append(i)
        elif role == "tool" and msg.get("tool_call_id"):
            native_tool_indices.append(i)

    recent_native: set[int] = (
        set(native_tool_indices[-stale_after_turns:]) if stale_after_turns > 0 else set()
    )
    recent_legacy: set[int] = (
        set(legacy_tool_indices[-stale_after_turns:]) if stale_after_turns > 0 else set()
    )
    pruned: List[Dict] = []

    for i, msg in enumerate(messages):
        content_raw = msg.get("content", "")
        has_legacy_marker = "tool_execution_result" in str(content_raw)
        if (
            msg.get("role") == "tool"
            and msg.get("tool_call_id")
            and not has_legacy_marker
            and i not in recent_native
        ):
            content_str = str(content_raw)
            if len(content_str) <= 400 or (
                current_step_hint and current_step_hint.lower() in content_str.lower()
            ):
                pruned.append(msg)
                continue
            tool_name = msg.get("name") or "tool"
            stub = json.dumps(
                {
                    "tool_name": tool_name,
                    "_pruned": True,
                    "_note": f"Full output pruned (stale - >{stale_after_turns} tool turns ago). Re-read if needed.",
                }
            )
            pruned.append({**msg, "content": stub})
        elif (
            msg.get("role") == "user"
            and has_legacy_marker
            and i not in recent_legacy
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
