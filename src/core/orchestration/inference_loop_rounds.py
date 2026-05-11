"""Graph-round execution helpers for the inference loop."""

from __future__ import annotations

import asyncio
import contextvars as _cv
from typing import Any, Dict, Optional

from src.core.logger import logger as guilogger


def _run_graph_round_sync(graph: Any, orch: Any, state_to_run: Dict[str, Any]) -> Dict[str, Any]:
    """Run one graph round synchronously for the provided state."""
    return asyncio.run(
        graph.ainvoke(
            state_to_run,
            {
                "configurable": {"orchestrator": orch},
                "recursion_limit": 50,
            },
        )
    )


def _execute_graph_round(
    *,
    graph: Any,
    orch: Any,
    graph_executor: Any,
    current_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Execute one graph round, reusing the orchestrator graph executor when needed."""
    try:
        asyncio.get_running_loop()
        _ctx = _cv.copy_context()
        future = graph_executor.submit(
            _ctx.run,
            _run_graph_round_sync,
            graph,
            orch,
            current_state,
        )
        return future.result()
    except RuntimeError:
        return _run_graph_round_sync(graph, orch, current_state)


def _analyze_round_result(final_state: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect one completed graph round and report loop-control signals."""
    last_assistant = ""
    assistant_msgs = [
        m["content"]
        for m in final_state.get("history", [])
        if m.get("role") == "assistant"
    ]
    if assistant_msgs:
        last_assistant = assistant_msgs[-1]

    try:
        from src.core.orchestration.tool_parser import parse_tool_block as _parse_tool_block

        has_tool_block = True if _parse_tool_block(last_assistant) else False
        if has_tool_block:
            guilogger.debug(
                "inference_loop: tool block detected in last assistant message"
            )
        else:
            guilogger.debug(
                f"inference_loop: no tool block in last assistant message (length={len(last_assistant)})"
            )
    except Exception as e:
        guilogger.warning(f"inference_loop: tool block check failed: {e}")
        has_tool_block = False

    history = final_state.get("history", [])
    last_assistant_idx = None
    for idx in range(len(history) - 1, -1, -1):
        if (
            history[idx].get("role") == "assistant"
            and history[idx].get("content") == last_assistant
        ):
            last_assistant_idx = idx
            break

    handled = False
    if last_assistant_idx is not None:
        for later in history[last_assistant_idx + 1 :]:
            if "tool_execution_result" in (later.get("content") or ""):
                handled = True
                break

    return {
        "last_assistant": last_assistant,
        "has_tool_block": has_tool_block,
        "handled": handled,
    }


def _prepare_next_round_state(
    *,
    final_state: Dict[str, Any],
    current_state: Dict[str, Any],
    orch: Any,
    cancel_event: Any,
) -> Dict[str, Any]:
    """Prepare the next graph-round state while preserving execution context."""
    _next_history = final_state.get("history", [])
    if _next_history and _next_history[-1].get("role") == "assistant":
        _next_history = list(_next_history) + [
            {
                "role": "user",
                "content": (
                    "Continue. If the task is already complete, "
                    "output STATUS: complete with no tool call."
                ),
            }
        ]

    _prev_working_dir = current_state.get("working_dir")
    _prev_system_prompt = current_state.get("system_prompt")
    return {
        **final_state,
        "history": _next_history,
        "verified_reads": final_state.get("verified_reads")
        or list(orch._session_read_files),
        "next_action": None,
        "last_result": None,
        "errors": [],
        "working_dir": _prev_working_dir or final_state.get("working_dir"),
        "system_prompt": _prev_system_prompt or final_state.get("system_prompt"),
        "deterministic": getattr(orch, "deterministic", False),
        "seed": getattr(orch, "seed", None),
        "cancel_event": cancel_event,
        "tool_last_used": final_state.get("tool_last_used") or {},
        "files_read": final_state.get("files_read") or {},
        "step_retry_counts": final_state.get("step_retry_counts") or {},
    }


def _build_loop_exit_response(
    *,
    final_state: Dict[str, Any],
    cancel_event: Any,
) -> Optional[Dict[str, Any]]:
    """Build early-return payloads for cancellation and loop-detected exits."""
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        guilogger.info("Orchestrator: Task was canceled, returning cancel response")
        return {
            "assistant_message": "[yellow]⚠ Task canceled by user.[/yellow]",
            "canceled": True,
        }

    if final_state:
        errors = final_state.get("errors", [])
        if any(e.startswith("infinite_loop") for e in errors):
            error_type = next(
                (e for e in errors if e.startswith("infinite_loop")),
                "infinite_loop",
            )
            guilogger.error(f"inference_loop: terminated due to {error_type}")

            if error_type == "infinite_loop_tool_limit":
                msg = (
                    "[red]⚠ Task stopped: Maximum tool call limit (5) reached.[/red]\n\n"
                    "The agent made too many tool calls without completing the task. "
                    "Try providing more specific instructions or breaking down the task."
                )
            else:
                msg = (
                    "[red]⚠ Task stopped: The agent entered an infinite loop and was terminated.[/red]\n\n"
                    "This may indicate the model is having trouble generating valid tool calls. "
                    "Try providing more specific instructions or a simpler task."
                )

            return {
                "assistant_message": msg,
                "loop_detected": True,
                "error_type": error_type,
                "final_state": final_state,
            }

    return None
