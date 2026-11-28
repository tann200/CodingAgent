"""Helpers for pruning and early-condensing stale tool outputs from prompt conversations."""

from __future__ import annotations

import json
from typing import Dict, List, Optional


_EARLY_CONDENSE_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "web_fetch",
        "web_search",
        "fetch_url",
        "grep",
        "search_code",
        "read_tool_output",
    }
)


def condense_stale_large_tool_outputs(
    messages: List[Dict],
    *,
    token_budget: Optional[int] = None,
    utilization_threshold: float = 0.50,
    stale_after_turns: int = 4,
    target_tools: frozenset[str] = _EARLY_CONDENSE_TOOLS,
    min_chars: int = 1_200,
) -> List[Dict]:
    """Condense stale bulky read/web/search outputs once history hits >=50% budget (OPT-05).

    Unlike hard 80% summary compaction, this lightweight pass only shrinks old
    (> *stale_after_turns* tool turns ago) large (>= *min_chars*) outputs from
    re-readable tools (``read_file``, ``web_fetch``, ``web_search``, ``grep``),
    keeping recent results, mutations, and ``metadata.preserve=True`` entries intact.
    """
    if not messages:
        return messages

    if token_budget is not None and token_budget > 0:
        est_tokens = sum(len(str(m.get("content") or "")) // 4 + 1 for m in messages)
        if est_tokens < int(token_budget * utilization_threshold):
            return messages

    tool_indices: List[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" or (
            msg.get("role") == "user"
            and "tool_execution_result" in str(msg.get("content") or "")
        ):
            tool_indices.append(i)

    recent_tools: set[int] = (
        set(tool_indices[-stale_after_turns:]) if stale_after_turns > 0 else set()
    )
    condensed: List[Dict] = []

    for i, msg in enumerate(messages):
        if i not in tool_indices or i in recent_tools:
            condensed.append(msg)
            continue
        meta = msg.get("metadata")
        if isinstance(meta, dict) and meta.get("preserve"):
            condensed.append(msg)
            continue

        content_str = str(msg.get("content") or "")
        if len(content_str) < min_chars or '"_condensed": true' in content_str or '"_pruned": true' in content_str:
            condensed.append(msg)
            continue

        tool_name = str(msg.get("name") or "").strip().lower()
        is_legacy = msg.get("role") == "user" and "tool_execution_result" in content_str
        if is_legacy and not tool_name:
            try:
                parsed = json.loads(content_str)
                res = parsed.get("tool_execution_result") or {}
                tool_name = str(res.get("tool_name") or res.get("name") or "").strip().lower()
            except Exception:
                tool_name = ""

        if tool_name not in target_tools:
            condensed.append(msg)
            continue

        note = (
            f"Condensed stale {tool_name} output ({len(content_str):,} chars, "
            f">{stale_after_turns} tool turns ago) at >={int(utilization_threshold * 100)}% "
            "context utilization; re-read if needed."
        )
        if is_legacy:
            stub = json.dumps(
                {
                    "tool_execution_result": {
                        "tool_name": tool_name,
                        "_condensed": True,
                        "original_chars": len(content_str),
                        "_note": note,
                    }
                }
            )
            condensed.append({**msg, "content": stub})
        else:
            stub = json.dumps(
                {
                    "tool_name": tool_name,
                    "_condensed": True,
                    "original_chars": len(content_str),
                    "_note": note,
                }
            )
            condensed.append({**msg, "content": stub})

    return condensed


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
