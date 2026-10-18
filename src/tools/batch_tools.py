"""
Batch tool — execute multiple tool calls in parallel via asyncio.gather.

Mirrors opencode's Batch tool: the LLM submits a list of {tool, input} pairs
and all calls run concurrently.  Results are returned in the same order as the
input list.

Constraints:
- Maximum 10 calls per batch (avoids runaway concurrency). This limit is enforced
- Nesting is blocked (a batch call cannot contain another batch call).
- Only tools registered in the current orchestrator may be invoked.

Usage by the orchestrator:
    Before calling the batch tool, set the thread-local context so the tool
    can dispatch sub-calls back through execute_tool:

        from src.tools.batch_tools import set_batch_orchestrator
        set_batch_orchestrator(self)
        result = self.tool_registry.get("batch")["fn"](calls=[...])
"""

from __future__ import annotations

import concurrent.futures
import contextvars as _cv
import logging
import threading
from typing import Any, Dict, List, Optional

from src.tools._tool import tool, PermissionKind

logger = logging.getLogger(__name__)

_BATCH_MAX_CALLS = 10
_batch_context = threading.local()
_BATCH_ORCHESTRATOR: _cv.ContextVar[Optional[Any]] = _cv.ContextVar(
    "_BATCH_ORCHESTRATOR", default=None
)


def set_batch_orchestrator(orchestrator: Any) -> Any:
    """Set the orchestrator for the current thread and context (called by execute_tool)."""
    _batch_context.orchestrator = orchestrator
    return _BATCH_ORCHESTRATOR.set(orchestrator)


def _get_orchestrator() -> Optional[Any]:
    orch = _BATCH_ORCHESTRATOR.get()
    if orch is not None:
        return orch
    local_orch = getattr(_batch_context, "orchestrator", None)
    if local_orch is not None:
        return local_orch
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        return _PARENT_ORCHESTRATOR_VAR.get(None)
    except Exception:
        return None


@tool(tags=["coding", "planning", "debug", "review"], permission_kind=PermissionKind.NONE)
def batch(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Execute multiple tool calls in parallel and return all results.

    Use when you need outputs from several independent read operations before
    deciding what to do next — for example: read three files at once, or run
    grep and list_files simultaneously.

    Args:
        calls: List of {"tool": "<tool_name>", "input": {...}} dicts.
               Maximum 10 entries.  Nesting (batch inside batch) is blocked.

    Returns:
        status ("ok" | "partial" | "error"), results (list ordered to match
        input), errors (list of failed entries).
    """
    if not isinstance(calls, list) or not calls:
        return {"status": "error", "error": "calls must be a non-empty list"}

    if len(calls) > _BATCH_MAX_CALLS:
        return {
            "status": "error",
            "error": f"Too many calls ({len(calls)}). Maximum is {_BATCH_MAX_CALLS}.",
        }

    for call in calls:
        if not isinstance(call, dict) or "tool" not in call:
            return {
                "status": "error",
                "error": f"Each call must have a 'tool' key. Got: {call!r}",
            }
        if call["tool"] == "batch":
            return {"status": "error", "error": "Nested batch calls are not allowed."}

    orchestrator = _get_orchestrator()
    if orchestrator is None:
        return {
            "status": "error",
            "error": "batch: no orchestrator context available. Ensure set_batch_orchestrator() was called.",
        }

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    try:
        # Run calls concurrently in a thread pool. Each task runs in its own
        # copied context to prevent "cannot enter context: already entered"
        # and ensure orchestrator contextvars propagate cleanly into workers.
        _max_workers = min(len(calls), 16)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_max_workers
        ) as pool:
            futs = [
                pool.submit(
                    _cv.copy_context().run,
                    orchestrator.execute_tool,
                    {"name": c.get("tool", ""), "arguments": c.get("input", {})},
                )
                for c in calls
            ]
            for c, fut in zip(calls, futs):
                try:
                    res = fut.result(timeout=120)
                    if isinstance(res, dict) and (
                        res.get("ok") is False or res.get("status") == "error"
                    ):
                        entry = {
                            "tool": c["tool"],
                            "status": "error",
                            "error": res.get("error") or str(res),
                            "result": res,
                        }
                        results.append(entry)
                        errors.append(entry)
                    else:
                        results.append(
                            {"tool": c["tool"], "status": "ok", "result": res}
                        )
                except Exception as exc:
                    entry = {
                        "tool": c["tool"],
                        "status": "error",
                        "error": str(exc),
                    }
                    results.append(entry)
                    errors.append(entry)

    except Exception as exc:
        return {"status": "error", "error": f"batch execution failed: {exc}"}

    return {
        "status": "error"
        if len(errors) == len(calls)
        else ("partial" if errors else "ok"),
        "results": results,
        "errors": errors,
    }
