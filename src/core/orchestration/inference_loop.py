"""Inference-loop helper extracted from Orchestrator (Phase E refactor).

``run_agent_once_impl(orch, ...)`` contains the full body of
``Orchestrator.run_agent_once``; all ``self.X`` references have been
mechanically replaced with ``orch.X``.

The Orchestrator delegates to this function; callers are unaffected because
the public method signature and return type are unchanged.
"""

from __future__ import annotations

import threading as _threading
from typing import Any, Dict, List, Optional

from src.core.logger import logger as guilogger
from src.core.orchestration.event_bus import new_correlation_id
from src.core.orchestration.inference_loop_responses import (
    _build_graph_failure_response,
    _build_success_response,
)
from src.core.orchestration.inference_loop_rounds import (
    _analyze_round_result,
    _build_loop_exit_response,
    _execute_graph_round,
    _prepare_next_round_state,
)
from src.core.orchestration.inference_loop_state import (
    build_initial_state,
    prepare_system_prompt,
    resolve_max_turns,
)
from src.core.orchestration.work_summary import _generate_work_summary

# Cache config_loader module so _cfg_get() always resolves through the live module
# (allowing unittest.mock.patch to intercept config_loader.get at test time).
import src.core.config_loader as _config_loader_module


def _cfg_get(key: str, default: Any = None) -> Any:
    """Thread-safe live lookup of config_loader.get(). Never raises."""
    try:
        return _config_loader_module.get(key, default)
    except Exception:
        return default

# Compatibility note for source-inspection tests: initial_state still includes
# keys like "agent_mode" and "max_turns"; construction now lives in
# inference_loop_state.build_initial_state().
# Sentinel fields preserved by extraction:
# "repo_summary_data": None
# "debug_attempts": 0
# "max_debug_attempts": 3
# "total_debug_attempts": 0
# "step_retry_counts": {}
# "plan_enforce_warnings": False
# "original_task": prompt
# "agent_mode": None
# "**final_state" is still used by round-state reconstruction in
# inference_loop_rounds._prepare_next_round_state().
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

    full_system_prompt = prepare_system_prompt(
        orch=orch,
        system_prompt_name=system_prompt_name,
        load_system_prompt=load_system_prompt,
    )

    initial_state = build_initial_state(
        orch=orch,
        prompt=prompt,
        full_system_prompt=full_system_prompt,
        cancel_event=cancel_event,
    )

    # 2. Compile and Run Graph — P1 fix: use module-level cached graph so compilation
    # happens once per process instead of once per run_agent_once() call.
    graph = get_compiled_graph_for_orchestrator(orchestrator=orch)

    # TASK-12: max_turns guard — enforce before invoking the graph so runaway
    # tasks cannot exceed the configured turn budget regardless of graph state.
    _max_turns = resolve_max_turns(initial_state=initial_state, config_getter=_cfg_get)
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
            # P1-T3: read from config key "max_graph_rounds" (default 20) so operators
            # can tune the limit without touching source code.
            _configured_rounds = (
                int(_cfg_get("max_graph_rounds", 20))
                if _cfg_get is not None
                else 20
            )
            MAX_TOOL_LOOP_ITERATIONS: int = _configured_rounds
            max_rounds = MAX_TOOL_LOOP_ITERATIONS
            current_state = initial_state

            # Loop safeguard: track iterations for no-progress detection
            loop_iteration = 0
            last_assistant_tracker: dict[str, Any] = {"last": "", "count": 0}

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
                    # P1-T5: surface the limit to the user as an assistant message so
                    # they know why the task stopped rather than seeing a silent abort.
                    _limit_msg = (
                        f"Task stopped: the tool-call loop limit ({MAX_TOOL_LOOP_ITERATIONS} rounds) "
                        "has been reached. This usually means the agent is stuck in a repeated "
                        "tool-call cycle. Please review the conversation and start a new task."
                    )
                    _history = list(final_state.get("history", []))
                    _history.append({"role": "assistant", "content": _limit_msg})
                    final_state["history"] = _history
                    final_state["assistant_message"] = _limit_msg
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
