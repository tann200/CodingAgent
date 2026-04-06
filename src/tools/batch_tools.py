"""
Batch tool — execute multiple tool calls in parallel via asyncio.gather.

Mirrors opencode's Batch tool: the LLM submits a list of {tool, input} pairs
and all calls run concurrently.  Results are returned in the same order as the
input list.

Constraints:
- Maximum 10 calls per batch (avoids runaway concurrency).
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

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from src.tools._tool import tool

logger = logging.getLogger(__name__)

_BATCH_MAX_CALLS = 10
_batch_context = threading.local()


def set_batch_orchestrator(orchestrator: Any) -> None:
    """Set the orchestrator for the current thread (called by execute_tool)."""
    _batch_context.orchestrator = orchestrator


def _get_orchestrator() -> Optional[Any]:
    return getattr(_batch_context, "orchestrator", None)


@tool(tags=["coding", "planning", "debug", "review"])
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

    async def _dispatch(call: Dict[str, Any]) -> Dict[str, Any]:
        tool_name = call["tool"]
        tool_input = call.get("input", {})
        try:
            result = orchestrator.execute_tool(
                {"name": tool_name, "arguments": tool_input}
            )
            return {"tool": tool_name, "status": "ok", "result": result}
        except Exception as exc:
            logger.warning(f"batch: {tool_name} raised: {exc}")
            return {"tool": tool_name, "status": "error", "error": str(exc)}

    async def _run_all() -> List[Dict[str, Any]]:
        return await asyncio.gather(*[_dispatch(c) for c in calls])

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # Inside an async context — run in thread pool to avoid deadlock
            import concurrent.futures

            # MED-2 fix: cap thread count to avoid spawning hundreds of threads for
            # large batches; an unbounded pool can exhaust OS thread limits.
            _max_workers = min(len(calls), 16)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=_max_workers
            ) as pool:
                futs = [
                    pool.submit(
                        orchestrator.execute_tool,
                        {"name": c["tool"], "arguments": c.get("input", {})},
                    )
                    for c in calls
                ]
                for i, (c, fut) in enumerate(zip(calls, futs)):
                    try:
                        res = fut.result(timeout=120)
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
        else:
            new_loop = asyncio.new_event_loop()
            try:
                raw = new_loop.run_until_complete(_run_all())
            finally:
                new_loop.close()
            for entry in raw:
                results.append(entry)
                if entry.get("status") == "error":
                    errors.append(entry)

    except Exception as exc:
        return {"status": "error", "error": f"batch execution failed: {exc}"}

    return {
        "status": "ok" if not errors else "partial",
        "results": results,
        "errors": errors,
    }
