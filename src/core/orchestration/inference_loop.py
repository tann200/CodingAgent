"""Inference-loop helper extracted from Orchestrator (Phase E refactor).

``run_agent_once_impl(orch, ...)`` contains the full body of
``Orchestrator.run_agent_once``; all ``self.X`` references have been
mechanically replaced with ``orch.X``.

The Orchestrator delegates to this function; callers are unaffected because
the public method signature and return type are unchanged.
"""

from __future__ import annotations

import asyncio
import contextvars as _cv
import json
import re
import threading as _threading
from typing import Any, Dict, List, Optional

from src.core.logger import logger as guilogger
from src.core.orchestration.event_bus import new_correlation_id
from src.core.orchestration.tool_result_formatter import (
    format_tool_result as _format_tool_result,
)
from src.core.inference.provider_utils import (
    resolve_provider_and_model as _resolve_provider_and_model,
)
from src.tools.subagent_payloads import build_graph_state_base
from src.core.orchestration.work_summary import _generate_work_summary


def _build_assistant_message(
    assistant_msgs: list,
    tool_results: list,
    last_tool_name: str | None,
) -> str:
    """Build the assistant message from tool results or raw assistant text.

    If the last assistant message is a tool call and we have results,
    format each result using the appropriate formatter.
    Otherwise, return the raw last assistant message.
    """
    if not assistant_msgs:
        return ""

    last_assistant = assistant_msgs[-1]

    # YAML blocks may start with ```yaml or bare "name:" (no fences).
    # LM Studio/Qwen models prefix the block with <think>...</think> — strip it first.
    _last_stripped = re.sub(
        r"<think>.*?</think>", "", last_assistant, flags=re.DOTALL
    ).strip()

    _is_tool_call_msg = (
        not last_assistant
        or _last_stripped.startswith("name:")
        or _last_stripped.startswith("```yaml")
        or _last_stripped.startswith("```\nname:")
        or (_last_stripped.startswith("```") and "name:" in _last_stripped)
    )

    if tool_results and _is_tool_call_msg:
        assistant_message = ""
        for i, result in enumerate(tool_results):
            tool_name = None
            if i == len(tool_results) - 1 and last_tool_name:
                tool_name = last_tool_name
            formatted = _format_tool_result(result, tool_name)
            if formatted:
                if assistant_message and not assistant_message.endswith("\n"):
                    assistant_message += "\n"
                assistant_message += formatted + "\n"
        return assistant_message.strip()
    else:
        return last_assistant


# ---------------------------------------------------------------------------
# GAP-9: Tier-aware max_turns helper
# ---------------------------------------------------------------------------


def _compute_default_max_turns(orch) -> int:
    """Return the tier-appropriate default max_turns for the active model.

    SMALL models (e.g. Gemma 4 E4B) exhaust context in ~25 turns.
    FRONTIER models (Gemma 4 31B/26B, Claude, GPT-4o) support 80-turn runs.
    Falls back to 50 if the model cannot be classified.

    The project-level maxTurns setting and --max-turns CLI flag still take
    precedence (applied later in perception_node and the TASK-12 guard).
    """
    try:
        from src.core.inference.model_tiers import classify_model, get_max_turns

        # Use the consolidated resolver which applies the shared
        # sanitisation heuristics (orch.get_provider_capabilities /
        # ProviderManager / adapter attributes).
        _, model = _resolve_provider_and_model(orch)
        if not model:
            return 50
        adapter = getattr(orch, "_adapter", None) or getattr(orch, "adapter", None)
        ctx_window = int(getattr(adapter, "context_window", 0) or 0)
        tier = classify_model(model, ctx_window)
        return get_max_turns(tier)
    except Exception:
        return 50


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
        # Fields that must be explicitly reset / refreshed each round:
        "history": _next_history,
        "verified_reads": final_state.get("verified_reads")
        or list(orch._session_read_files),
        "next_action": None,
        "last_result": None,
        "errors": [],
        # Execution context fields — keep stable from initial invocation
        "working_dir": _prev_working_dir or final_state.get("working_dir"),
        "system_prompt": _prev_system_prompt or final_state.get("system_prompt"),
        "deterministic": getattr(orch, "deterministic", False),
        "seed": getattr(orch, "seed", None),
        "cancel_event": cancel_event,
        # Cooldown / read-tracking dicts must not be None
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


def _sync_msg_mgr_with_final_history(orch: Any, final_state: Dict[str, Any]) -> None:
    """Append only new graph-produced turns into the MessageManager."""
    if not final_state or "history" not in final_state:
        return

    msg_count_before = len(orch.msg_mgr.messages)
    if len(final_state["history"]) <= msg_count_before:
        return

    new_turns = final_state["history"][msg_count_before:]
    for turn in new_turns:
        orch.msg_mgr.append(turn["role"], turn["content"])


def _extract_assistant_and_tool_results(
    final_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract assistant messages and parsed tool results from final history."""
    assistant_msgs = [
        m["content"] for m in final_state.get("history", []) if m["role"] == "assistant"
    ]
    tool_results = []
    last_tool_name = None

    for i, m in enumerate(final_state.get("history", [])):
        is_tool_result = m.get("role") == "tool" or (
            m.get("role") == "user"
            and "tool_execution_result" in (m.get("content") or "")
        )
        if not is_tool_result:
            continue

        content = m.get("content", "")
        tool_name = None
        if i > 0:
            prev_msg = final_state["history"][i - 1]
            if prev_msg.get("role") == "assistant":
                try:
                    from src.core.orchestration.tool_parser import parse_tool_block

                    parsed = parse_tool_block(prev_msg.get("content", ""))
                    if parsed and parsed.get("name"):
                        tool_name = parsed["name"]
                except Exception:
                    pass

        if "tool_execution_result" in content:
            try:
                data = json.loads(content)
                if isinstance(data, dict) and "tool_execution_result" in data:
                    ter = data["tool_execution_result"]
                    if isinstance(ter, dict) and "result" in ter:
                        result = ter["result"]
                    elif isinstance(ter, dict):
                        result = ter
                    else:
                        result = ter
                    tool_results.append(result)
                    if tool_name:
                        last_tool_name = tool_name
                elif isinstance(data, dict) and "result" in data:
                    tool_results.append(data["result"])
                    if tool_name:
                        last_tool_name = tool_name
                elif isinstance(data, dict) and data.get("ok"):
                    tool_results.append(data)
                    if tool_name:
                        last_tool_name = tool_name
            except (json.JSONDecodeError, TypeError):
                tool_results.append(content)
        elif content:
            tool_results.append(content)

    return {
        "assistant_msgs": assistant_msgs,
        "tool_results": tool_results,
        "last_tool_name": last_tool_name,
    }


def _build_success_response(orch: Any, final_state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the successful run_agent_once response from final graph state."""
    if final_state:
        guilogger.info(
            f"Final state verified reads: {final_state.get('verified_reads')}"
        )

    _sync_msg_mgr_with_final_history(orch, final_state)

    extracted = _extract_assistant_and_tool_results(final_state)
    assistant_msgs = extracted["assistant_msgs"]
    tool_results = extracted["tool_results"]
    last_tool_name = extracted["last_tool_name"]

    guilogger.info(
        f"Graph execution completed in {final_state.get('rounds', 0) if final_state else 0} rounds"
        if final_state
        else "Graph execution completed"
    )

    history = final_state.get("history", []) if final_state else []
    work_summary = _generate_work_summary(final_state, history)
    assistant_message = _build_assistant_message(
        assistant_msgs, tool_results, last_tool_name
    )

    delegation_results = final_state.get("delegation_results") if final_state else None
    if delegation_results:
        guilogger.info(
            f"run_agent_once: delegation_results keys={list(delegation_results.keys())}"
        )

    orch.cost_tracker.flush(task_id=getattr(orch, "_current_task_id", ""))
    orch.flush_execution_trace()

    if assistant_message:
        try:
            _sid = getattr(orch, "_current_task_id", None)
            try:
                _tname = _threading.current_thread().name
            except Exception:
                _tname = "unknown"
            guilogger.debug(
                "inference_loop: add_message (session=%r, role=%s, thread=%s)",
                _sid,
                "assistant",
                _tname,
            )
            orch.session_store.add_message(
                session_id=_sid,
                role="assistant",
                content=assistant_message.strip(),
            )
        except Exception:
            pass

    return {
        "assistant_message": assistant_message.strip(),
        "work_summary": work_summary,
        "delegation_results": delegation_results or {},
        "dry_run_intercepted": list(getattr(orch, "_dry_run_log", [])),
    }


def _call_model_fallback_sync(
    *,
    orch: Any,
    prompt: str,
    full_system_prompt: str,
    streaming_enabled: bool,
) -> Any:
    """Call the model directly as a best-effort fallback after graph failure."""
    from src.core.inference.llm_manager import call_model

    provider_name = None
    model_name = None
    try:
        provider_name, model_name = _resolve_provider_and_model(orch)
    except Exception:
        provider_name, model_name = None, None

    messages_for_model = [
        {"role": "system", "content": full_system_prompt},
        {"role": "user", "content": prompt},
    ]
    try:
        _call_coro = call_model(
            messages_for_model,
            provider=provider_name,
            model=model_name,
            stream=streaming_enabled,
            format_json=False,
        )
        try:
            asyncio.get_running_loop()

            _ctx = _cv.copy_context()
            _fb_executor = getattr(orch, "_graph_executor", None)
            if _fb_executor is not None:
                return _fb_executor.submit(_ctx.run, asyncio.run, _call_coro).result()

            import concurrent.futures as _cf_fb

            with _cf_fb.ThreadPoolExecutor(max_workers=1) as _ex:
                return _ex.submit(_ctx.run, asyncio.run, _call_coro).result()
        except RuntimeError:
            return asyncio.run(_call_coro)
    except Exception:
        return None


def _extract_fallback_response_content(resp: Any) -> str:
    """Extract assistant content from a direct call_model fallback response."""
    content = ""
    if isinstance(resp, dict):
        _choices = resp.get("choices")
        if _choices and len(_choices) > 0:
            ch = _choices[0].get("message") if isinstance(_choices[0], dict) else None
        else:
            ch = resp.get("message")
        if isinstance(ch, str):
            content = ch
        elif isinstance(ch, dict):
            content = ch.get("content") or ""
    return content


def _build_graph_failure_response(
    *,
    orch: Any,
    error: Exception,
    prompt: str,
    full_system_prompt: str,
    streaming_enabled: bool,
) -> Dict[str, Any]:
    """Build the fallback response after graph execution fails."""
    guilogger.error(f"Graph execution failed: {error}")
    orch.msg_mgr.append("user", f"Error during tool execution: {error}")

    try:
        resp = _call_model_fallback_sync(
            orch=orch,
            prompt=prompt,
            full_system_prompt=full_system_prompt,
            streaming_enabled=streaming_enabled,
        )
        content = _extract_fallback_response_content(resp)

        if content:
            try:
                orch.msg_mgr.append("assistant", content)
            except Exception:
                pass

        orch.cost_tracker.flush(task_id=getattr(orch, "_current_task_id", ""))
        orch.flush_execution_trace()
        return {
            "assistant_message": content if content else "",
            "error": "graph_failed",
            "exception": str(error),
        }
    except Exception:
        orch.cost_tracker.flush(task_id=getattr(orch, "_current_task_id", ""))
        orch.flush_execution_trace()
        return {"error": "graph_failed", "exception": str(error)}


# ---------------------------------------------------------------------------
# run_agent_once_impl
# ---------------------------------------------------------------------------
# _generate_work_summary — copied verbatim from orchestrator.py
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# run_agent_once_impl
# ---------------------------------------------------------------------------
# run_agent_once_impl
# ---------------------------------------------------------------------------


def run_agent_once_impl(
    orch,
    system_prompt_name: Optional[str],
    messages: List[Dict[str, Any]],
    tools: Dict[str, Any],
    cancel_event: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Invokes the LangGraph cognitive pipeline to execute the task.
    """
    # G12: opt-in token streaming — mirrors llm_helpers._STREAMING_ENABLED
    try:
        from src.core.inference.llm_helpers import (
            _STREAMING_ENABLED as _streaming_enabled,
        )
    except Exception:
        _streaming_enabled = False

    # Store cancel_event on orchestrator instance so nodes can access it via getattr
    orch.cancel_event = cancel_event

    # F16: Reset session read-file tracking at the start of each new task so reads
    # from a previous task cannot bypass the read-before-edit guard in a new task.
    orch._session_read_files = set()
    # F17: Reset per-task usage buffer; will be flushed to disk once at task end.
    orch._usage_buffer = {}
    # D-10: Reset the SessionCostTracker buffer and idempotency guard for this turn.
    orch.cost_tracker.reset()
    orch.tool_execution_service.reset_idempotency()
    # UX-3: Reset dry-run intercept log for this run.
    orch._dry_run_log = []  # type: ignore[attr-defined]

    # Check if canceled before starting
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        return {
            "ok": False,
            "error": "canceled_before_start",
            "assistant_message": "Task was canceled before starting.",
        }

    prompt = ""
    if messages and isinstance(messages, list) and messages[-1].get("role") == "user":
        prompt = messages[-1].get("content", "")

    # SES-W2: Persist user prompt to SessionStore transcript.
    if prompt:
        try:
            _sid = getattr(orch, "_current_task_id", None)
            try:
                _tname = _threading.current_thread().name
            except Exception:
                _tname = "unknown"
            guilogger.debug(
                "inference_loop: add_message (session=%r, role=%s, thread=%s)",
                _sid,
                "user",
                _tname,
            )
            orch.session_store.add_message(
                session_id=_sid,
                role="user",
                content=prompt,
            )
        except Exception:
            pass

    # ORCH-W5: Generate session title via the internal "title" agent (one-shot,
    # no tool loop).  Only on the first turn (_session_title not yet set) so we
    # don't regenerate on every continuation call.  Fire-and-forget in a daemon
    # thread so it never blocks the main pipeline.
    if prompt and not getattr(orch, "_session_title", None):

        def _gen_title(p: str) -> None:
            try:
                from src.core.memory.distiller import generate_session_title

                _t = generate_session_title(p)
                if _t:
                    orch._session_title = _t
                    orch.event_bus.publish("session.title_generated", {"title": _t})
            except Exception:
                pass

        # SEC-VOL23-1: store thread reference on the orchestrator so the shutdown
        # path can join it with a short timeout, preventing a partial write if
        # the process exits while the title is still being generated.
        _t_thread = _threading.Thread(target=_gen_title, args=(prompt,), daemon=True)
        _t_thread.start()
        orch._session_title_thread = _t_thread

    from src.core.orchestration.agent_brain import load_system_prompt
    from src.core.orchestration.graph.builder import (
        get_compiled_graph_for_orchestrator,
    )

    # 1. Prepare Initial State
    # Ensure current model routing is published in case tests replace the event_bus after instantiation
    try:
        orch._publish_active_config()
    except Exception:
        pass

    # Probe the active adapter for context window length so get_context_budget()
    # reflects the currently loaded model even in headless (no-TUI) mode.
    try:
        from src.core.inference.llm_manager import get_provider_manager
        from src.core.inference.provider_context import set_active_context_length

        pm = get_provider_manager()
        active_adapter = (
            pm.get_active_adapter() if hasattr(pm, "get_active_adapter") else None
        )
        if active_adapter and hasattr(active_adapter, "get_loaded_context_length"):
            active_models = (
                pm.get_active_models() if hasattr(pm, "get_active_models") else []
            )
            model_name = active_models[0] if active_models else ""
            ctx_len = active_adapter.get_loaded_context_length(model_name)
            if ctx_len and ctx_len > 0:
                active_provider = ""
                try:
                    active_provider = pm.get_active_provider_name() or ""
                except Exception:
                    pass
                set_active_context_length(ctx_len, provider_id=active_provider)
    except Exception:
        pass

    full_system_prompt = (
        load_system_prompt(system_prompt_name) or "You are a helpful coding assistant."
    )

    # Append live git context + project instruction files (P1 gap fixes)
    try:
        from src.core.orchestration.instruction_loader import build_runtime_context

        runtime_ctx = build_runtime_context(cwd=orch.working_dir)
        if runtime_ctx:
            full_system_prompt = full_system_prompt + runtime_ctx
    except Exception:
        pass

    # Ensure the MessageManager contains the current system prompt (replace if different)
    try:
        orch.msg_mgr.set_system_prompt(full_system_prompt)
    except Exception:
        pass

    initial_state = {
        **build_graph_state_base(
            task=prompt,
            session_id=orch._current_task_id,
            working_dir=str(orch.working_dir),
            system_prompt=full_system_prompt,
            history=orch.msg_mgr.messages,
            verified_reads=list(orch._session_read_files),
            parent_session_id=None,
            delegation_depth=0,
        ),
        # deterministic hints for nodes
        "deterministic": getattr(orch, "deterministic", False),
        "cancel_event": cancel_event,
        "seed": getattr(orch, "seed", None),
        # infinite loop prevention
        "empty_response_count": 0,
        # analysis phase
        "analysis_summary": None,
        "relevant_files": [],
        "key_symbols": [],
        # VOL7-3: repo_summary_data generated by analysis_node; initialised
        # here so downstream nodes never encounter a missing key.
        "repo_summary_data": None,
        # debug retry tracking
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        # VOL7-1: total_debug_attempts was missing — the global cap in
        # should_after_evaluation (MAX_TOTAL_DEBUG=9) relies on this field.
        # Without initialisation the counter starts at None → coerces to 0
        # on the first debug cycle but may not persist cleanly across cycles.
        "total_debug_attempts": 0,
        # verification result
        "verification_passed": None,
        "verification_result": None,
        # step controller
        "step_controller_enabled": True,
        # task decomposition
        "task_decomposed": False,
        # tool call budgeting
        "tool_call_count": 0,
        "max_tool_calls": 30,
        # Tool cooldown tracker: keyed by "tool_name:path_arg", value = tool_call_count at last use
        "tool_last_used": {},
        # Fast read-before-edit lookup: resolved_path → True
        "files_read": {},
        # ME-1 fix: analyst_findings was missing from initial_state — analyst_delegation_node
        # writes this field and planning_node reads it; a missing initial value causes
        # a KeyError on the first planning pass when no delegation has run yet.
        "analyst_findings": None,
        # plan resumption flag written by planning_node
        "plan_resumed": False,
        # Plan Mode fields
        "plan_mode_enabled": getattr(
            getattr(orch, "plan_mode", None), "enabled", False
        ),
        "awaiting_plan_approval": False,
        "plan_mode_approved": None,
        "plan_mode_blocked_tool": None,
        "needs_clarification": None,  # GAP-SMALL-4
        # PRSW: FileLockManager reference
        "_file_lock_manager": getattr(orch, "file_lock_manager", None),
        # PRSW: Pending write operations (empty at task start)
        "_write_queue": [],
        # Phase B: P2P session tracking (singleton references, not serialised)
        "_agent_session_manager": getattr(orch, "agent_session_manager", None),
        "_agent_messages": [],
        "_context_controller": getattr(orch, "context_controller", None),
        # Phase 4: Token auto-compact tracking
        "last_compact_at": None,
        "last_compact_turn": 0,
        "context_degradation_detected": False,
        # P1-2/P1-3: Inner-loop counters
        "plan_attempts": 0,
        "replan_attempts": 0,
        # P2-A: Global recovery cap across debug + replan (prevents alternating loops)
        "total_recovery_attempts": 0,
        # P1-6: Warnings are advisory only — enforce_warnings=True triggers infinite
        # replanning loops because any plan without an explicit test/verify step is
        # rejected. The plan_attempts guard (>=3 → force execution) was added as a
        # band-aid but the recursion limit is hit before it fires. Keep False.
        "plan_enforce_warnings": False,
        # Turn limit: independent of tool_call_count; bounded by max_turns
        # GAP-9: Use tier-aware default so small models (NANO/SMALL) stop before
        # exhausting their context window, and frontier models can run longer.
        "turn_count": 0,
        "max_turns": _compute_default_max_turns(orch),
        "plan_strict_mode": False,
        # P3-1: Structured dependency data from analysis phase
        "call_graph": None,
        "test_map": None,
        # Doom loop detection: fingerprints of last N tool calls
        "recent_tool_calls": [],
        # Phase A: DAG execution fields (populated by planning_node)
        "plan_dag": None,
        "execution_waves": None,
        "current_wave": 0,
        # Phase 3: Preview Mode
        "preview_mode_enabled": False,
        "pending_preview_id": None,
        "awaiting_user_input": False,
        "preview_confirmed": None,
        # Token Auto-Compact
        "_should_distill": None,
        "_force_compact": None,
        "_budget_compaction": None,
        # P2P context buffering
        "_p2p_context": None,
        # Fields present in AgentState but omitted from prior initial_state versions;
        # explicit None/0/{} prevents KeyError in nodes that assume presence.
        "action_failed": None,
        "delegation_depth": 0,
        "evaluation_result": None,
        "last_debug_error_type": None,
        "last_tool_name": None,
        "no_plan_fail_count": 0,
        # DOOM-LOOP FIX: Always populate original_task from the user prompt so
        # nodes can safely use state["original_task"] as the authoritative
        # task string regardless of what planning_node does to state["task"].
        "original_task": prompt,
        "plan_progress": None,
        "plan_validation": None,
        "planned_action": None,
        "replan_required": None,
        "step_description": None,
        "step_retry_counts": {},
        "task_history": None,
        # S4-A: Snapshot tree-hash list — appended by perception_node each turn.
        "snapshots": [],
        # ORCH-W4: Current agent operating mode ("execution" or "planning").
        # Changed by plan_enter / plan_exit tool calls.
        "agent_mode": None,
        # SPAWN-W1: Parent session ID for delegated child sessions.  None = top-level.
        "parent_session_id": None,
        # MID-INJ: Source object for mid-run user message injection.
        # Set by core_bridge before calling run_agent_once (orch._injection_source).
        # perception_node calls .pop_pending_injections() each round.
        "_pending_injections_source": getattr(orch, "_injection_source", None),
    }

    # 2. Compile and Run Graph — P1 fix: use module-level cached graph so compilation
    # happens once per process instead of once per run_agent_once() call.
    graph = get_compiled_graph_for_orchestrator(orchestrator=orch)

    # TASK-12: max_turns guard — enforce before invoking the graph so runaway
    # tasks cannot exceed the configured turn budget regardless of graph state.
    _max_turns = int(initial_state.get("max_turns") or 50)
    try:
        from src.core.config_loader import get as _cfg_get

        _cfg_max = _cfg_get("max_turns")
        if _cfg_max is not None:
            _max_turns = int(_cfg_max)
    except Exception:
        pass
    _turn_count = int(initial_state.get("turn_count") or 0)
    if _turn_count >= _max_turns:
        guilogger.warning(
            f"run_agent_once: turn_count={_turn_count} >= max_turns={_max_turns} — refusing to start new turn"
        )
        return {
            "ok": False,
            "error": f"max_turns limit reached ({_turn_count}/{_max_turns}).",
            "assistant_message": (
                f"Task aborted: the maximum number of turns ({_max_turns}) has been "
                "reached. Start a new session to continue."
            ),
        }

    # Mint a fresh correlation ID for this agent turn so all EventBus events
    # and LLM call logs share the same trace token (#26).
    cid = new_correlation_id()
    guilogger.info(f"run_agent_once: starting with task: {prompt[:80]} [cid={cid}]")

    # Initialize final_state before try to satisfy LSP
    final_state: dict = {}

    try:
        # MED-5 fix: reuse the instance-level _graph_executor instead of
        # creating (and destroying) a new OS thread pool per run_agent_once() call.
        # The executor is created in __init__ and shut down in close().

        _graph_executor = orch._graph_executor

        try:
            # Allow multiple graph rounds to consume multi-turn tool sequences (bounded)
            # F-71: single named constant; guard below uses >= so it fires at exactly
            # MAX_TOOL_LOOP_ITERATIONS, not one beyond it.
            MAX_TOOL_LOOP_ITERATIONS: int = 5
            max_rounds = MAX_TOOL_LOOP_ITERATIONS
            current_state = initial_state

            # Loop safeguard: track iterations for no-progress detection
            loop_iteration = 0
            last_assistant_tracker = {"last": "", "count": 0}

            for round_idx in range(max_rounds):
                # Check for cancellation at the start of each round
                if (
                    cancel_event
                    and hasattr(cancel_event, "is_set")
                    and cancel_event.is_set()
                ):
                    guilogger.info(
                        "Orchestrator: Task canceled by user during round loop"
                    )
                    break

                guilogger.debug(
                    f"Starting graph round {round_idx} (iteration {loop_iteration})"
                )

                next_state = _execute_graph_round(
                    graph=graph,
                    orch=orch,
                    graph_executor=_graph_executor,
                    current_state=current_state,
                )

                guilogger.info(
                    f"Graph round {round_idx}: next_state keys: {list(next_state.keys()) if next_state else 'None'}"
                )

                # If nothing changed (no new assistant turn) or no next action, stop early
                final_state = next_state

                round_result = _analyze_round_result(final_state)
                last_assistant = round_result["last_assistant"]
                has_tool_block = bool(round_result["has_tool_block"])
                handled = bool(round_result["handled"])

                # If there's no unhandled tool block, we're done
                if not has_tool_block or handled:
                    content_preview = (last_assistant or "")[:200]
                    guilogger.debug(
                        "inference_loop: no unhandled tool block, exiting loop. "
                        "Content preview: %r",
                        content_preview,
                    )
                    break

                # Increment iteration counter and check for infinite loop
                loop_iteration += 1

                # STRICT LIMIT: Never allow more than MAX_TOOL_LOOP_ITERATIONS tool call iterations
                if loop_iteration >= MAX_TOOL_LOOP_ITERATIONS:
                    guilogger.error(
                        f"inference_loop: TOOL LOOP LIMIT EXCEEDED - {loop_iteration} iterations (max {MAX_TOOL_LOOP_ITERATIONS})"
                    )
                    final_state = final_state or {}
                    final_state["errors"] = list(final_state.get("errors", [])) + [
                        "infinite_loop_tool_limit"
                    ]
                    break

                # Track last assistant message for no-progress detection
                if last_assistant_tracker["last"] == last_assistant:
                    last_assistant_tracker["count"] += 1
                else:
                    last_assistant_tracker["count"] = 0
                    last_assistant_tracker["last"] = last_assistant

                guilogger.info(
                    f"inference_loop: iteration {loop_iteration}/{MAX_TOOL_LOOP_ITERATIONS}, "
                    f"history length={len(final_state.get('history', []))}"
                )

                # SCAN3-1 fix: preserve ALL state from the completed round and
                # only override the fields that must reset for the next round.
                # Prior hand-rolled reconstruction silently discarded wave state,
                # analysis results, loop counters, delegation state, and more.
                current_state = _prepare_next_round_state(
                    final_state=final_state,
                    current_state=current_state,
                    orch=orch,
                    cancel_event=cancel_event,
                )
        finally:
            # MED-5 fix: _graph_executor is now instance-level and shut down in
            # close() — do NOT shut it down here or subsequent calls will fail.
            pass

        loop_exit_response = _build_loop_exit_response(
            final_state=final_state,
            cancel_event=cancel_event,
        )
        if loop_exit_response is not None:
            return loop_exit_response

        return _build_success_response(orch, final_state)
    except StopAsyncIteration:
        # This is expected when the graph finishes successfully.
        history = (
            final_state.get("history", []) if isinstance(final_state, dict) else []
        )
        # Add session modified files to final_state for work summary
        if isinstance(final_state, dict):
            final_state["_session_modified_files"] = list(orch._session_modified_files)
            # Publish session changes for sidebar
            orch._publish_session_changes()
        work_summary = _generate_work_summary(final_state, history)
        orch.cost_tracker.flush(task_id=getattr(orch, "_current_task_id", ""))
        orch.flush_execution_trace()
        return {
            "assistant_message": "Graph finished.",
            "work_summary": work_summary,
            "dry_run_intercepted": list(getattr(orch, "_dry_run_log", [])),
        }
    except Exception as e:
        return _build_graph_failure_response(
            orch=orch,
            error=e,
            prompt=prompt,
            full_system_prompt=full_system_prompt,
            streaming_enabled=_streaming_enabled,
        )
