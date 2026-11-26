"""Parallel read-only ``/btw <question>`` side-query runner (TASK-36).

Executes an ephemeral read-only query over a frozen copy of the current
conversation snapshot using only pure read tools (``read_file``, ``grep``,
``list_files``, ``find_files``). Never mutates the main session's message
history or workspace files.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from src.core.security.redaction import redact_secrets

BTW_ALLOWED_TOOLS = frozenset({"read_file", "grep", "list_files", "find_files"})


def _build_btw_messages(
    question: str,
    history_snapshot: Sequence[Mapping[str, Any]],
    *,
    max_history_messages: int = 12,
) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are answering a quick parallel side-question (/btw) for the developer. "
                "You are strictly read-only (only read_file, grep, list_files, find_files are permitted). "
                "Give a concise, direct answer without modifying any files."
            ),
        }
    ]
    # Freeze a shallow-redacted copy of recent user/assistant turns without touching caller's history
    tail = list(history_snapshot[-max_history_messages:]) if history_snapshot else []
    for item in tail:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = item.get("content", "")
        if isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": redact_secrets(content[:1500])})

    messages.append({"role": "user", "content": redact_secrets(question.strip())})
    return messages


def filter_btw_tools(tools: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Return deep copies of only the allowlisted read-only tools for ``/btw``."""
    allowed: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, Mapping):
            continue
        fn = t.get("function")
        name = str(fn.get("name") if isinstance(fn, Mapping) else t.get("name") or "").strip()
        if name in BTW_ALLOWED_TOOLS:
            allowed.append(copy.deepcopy(dict(t)))
    return allowed


def run_btw_query(
    question: str,
    *,
    history_snapshot: Sequence[Mapping[str, Any]] = (),
    tools: Sequence[Mapping[str, Any]] = (),
    provider: Optional[str] = None,
    model: Optional[str] = None,
    call_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Run an ephemeral read-only ``/btw`` query without mutating *history_snapshot*."""
    q = str(question or "").strip()
    if not q:
        return {"status": "error", "error": "Usage: /btw <question>"}

    # Freeze history snapshot so caller's list can never be mutated
    frozen_history = copy.deepcopy(list(history_snapshot))
    messages = _build_btw_messages(q, frozen_history)
    ro_tools = filter_btw_tools(tools)

    fn = call_fn
    if fn is None:
        from src.core.inference.llm_manager import call_model

        fn = call_model

    try:
        resp = fn(
            messages,
            provider=provider,
            model=model,
            tools=ro_tools,
            stream=False,
        )
    except Exception as exc:
        return {
            "status": "error",
            "error": f"/btw query failed: {redact_secrets(str(exc))}",
        }

    answer = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            if isinstance(msg, dict) and msg.get("content"):
                answer = str(msg["content"])
        elif resp.get("content"):
            answer = str(resp["content"])
    elif isinstance(resp, str):
        answer = resp
    else:
        answer = str(resp or "")

    return {
        "status": "ok",
        "question": q,
        "answer": redact_secrets(answer.strip() or "(no response)"),
        "allowed_tools": sorted(BTW_ALLOWED_TOOLS),
        "history_mutated": False,
    }


__all__ = ["BTW_ALLOWED_TOOLS", "filter_btw_tools", "run_btw_query"]
