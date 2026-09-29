"""frontier_toolcalls.py — tool-call shaping helpers for the frontier loop.

Split out of ``frontier_loop_node`` to keep that node within its size guard and
to move it toward the 600-line target recorded in ``test_module_sizes``.

These are pure functions with no state and no I/O: they normalize a raw LLM tool
call into the ``(name, args)`` pair the dispatcher expects, serialize tool calls
back into assistant history in the parseable form ``parse_tool_block``
understands, and append or merge that assistant turn.

Bodies are carried over verbatim from ``frontier_loop_node``; only the
module-level imports moved.  Behavior is covered by
``tests/unit/test_frontier_loop_node.py`` (re-exported names) and
``tests/unit/test_frontier_tool_output_truncation.py``.
"""

from __future__ import annotations

import json
from typing import Any

from src.core.orchestration.tool_parser import parse_tool_block


def _normalize_tool_call(tc: Any) -> tuple[str, dict]:
    """Return a normalized (tool_name, tool_args) tuple."""
    if isinstance(tc, dict):
        tool_name = (
            tc.get("name")
            or tc.get("tool")
            or (tc.get("function", {}) or {}).get("name", "")
        )
        raw_args = (
            tc.get("args")
            or tc.get("arguments")
            or tc.get("input")
            or (tc.get("function", {}) or {}).get("arguments", {})
            or {}
        )
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except Exception:
                raw_args = {}
        return tool_name, raw_args if isinstance(raw_args, dict) else {}

    try:
        tool_name = tc.function.name
        raw_args = tc.function.arguments
        tool_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        return tool_name, tool_args if isinstance(tool_args, dict) else {}
    except Exception:
        return "", {}


def _render_tool_calls_text(tool_calls: list[Any]) -> str:
    """Serialize tool calls into parseable assistant history content."""
    blocks: list[str] = []
    for tc in tool_calls:
        tool_name, tool_args = _normalize_tool_call(tc)
        if not tool_name:
            continue
        try:
            blocks.append(
                json.dumps(
                    {"name": tool_name, "arguments": tool_args},
                    ensure_ascii=True,
                    default=str,
                )
            )
        except Exception:
            continue
    return "\n".join(blocks)


def _append_tool_call_history(history: list[dict], tool_calls: list[Any]) -> list[dict]:
    """Ensure history contains a parseable assistant tool-call record."""
    rendered = _render_tool_calls_text(tool_calls)
    if not rendered:
        return history

    updated = list(history)
    if updated and updated[-1].get("role") == "assistant":
        prior_content = updated[-1].get("content") or ""
        if not parse_tool_block(prior_content):
            merged = (
                f"{prior_content.rstrip()}\n\n{rendered}" if str(prior_content).strip() else rendered
            )
            updated[-1] = {**updated[-1], "content": merged}
            return updated

    updated.append({"role": "assistant", "content": rendered})
    return updated
