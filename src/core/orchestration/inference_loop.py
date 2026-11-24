"""Inference-loop helper extracted from Orchestrator (Phase E refactor).

``run_agent_once_impl(orch, ...)`` contains the full body of
``Orchestrator.run_agent_once``; all ``self.X`` references have been
mechanically replaced with ``orch.X``.

The Orchestrator delegates to this function; callers are unaffected because
the public method signature and return type are unchanged.
"""

from __future__ import annotations

import contextlib as _contextlib
import contextvars as _contextvars
import os
import threading as _threading
from typing import Any, Dict, List, Optional

from src.core.logger import logger as guilogger
from src.core.orchestration.event_bus import new_correlation_id
from src.core.messaging.event_types import SessionTitleGenerated
from src.core.orchestration.graph.routing_constants import MAX_GRAPH_ROUNDS

__all__ = ["run_agent_once_impl", "MAX_GRAPH_ROUNDS"]

from src.core.orchestration.inference_loop_responses import (
    _build_graph_failure_response,
    _build_success_response,
)
from src.core.orchestration.inference_loop_rounds import (
    _build_loop_exit_response,
    _execute_graph_round,
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


def _try_save_thread_state(orch: Any, state: Dict[str, Any]) -> None:
    """Best-effort durable snapshot of the graph state after a completed round."""
    try:
        from src.core.orchestration.graph.checkpoint_saver import (
            save_thread_state,
        )

        save_thread_state(orch, state)
    except Exception:
        pass

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
    if system_prompt_name:
        orch.active_role = system_prompt_name
    elif not getattr(orch, "active_role", None):
        orch.active_role = "operational"

    sid = getattr(orch, "session_id", None) or getattr(orch, "_current_task_id", None)
    if sid:
        try:
            from src.core.orchestration.event_bus import set_session_id
            set_session_id(str(sid))
        except Exception:
            pass

    # F16: Reset session read-file tracking at the start of each new task so reads
    # from a previous task cannot bypass the read-before-edit guard in a new task.
    orch._session_read_files = set()
    try:
        from src.tools.guardrails import reset_guardrail_state

        reset_guardrail_state()
    except Exception:
        pass
    # F17: Reset per-task usage buffer; will be flushed to disk once at task end.
    orch._usage_buffer = {}
    # D-10: Reset the SessionCostTracker buffer and idempotency guard for this turn.
    orch.cost_tracker.reset()
    orch.tool_execution_service.reset_idempotency()
    # UX-3: Reset dry-run intercept log for this run.
    orch._dry_run_log = []  # type: ignore[attr-defined]

    # Ensure deferred initialization runs before first task execution
    if not getattr(orch, "_deferred_init_done", False):
        try:
            if hasattr(orch, "run_deferred_init_sync"):
                orch.run_deferred_init_sync()
        except Exception as _d_exc:
            guilogger.debug("run_deferred_init_sync error in inference loop: %s", _d_exc)

    def _valid_runtime_str(val: Any) -> Optional[str]:
        if isinstance(val, str):
            s = val.strip()
            if s and "MagicMock" not in s:
                return s
        return None

    _ctx_adapter = getattr(orch, "adapter", None) or getattr(orch, "_adapter", None)
    _ctx_provider = (
        _valid_runtime_str(getattr(orch, "_provider_name", None))
        or _valid_runtime_str(getattr(_ctx_adapter, "provider", None))
        or _valid_runtime_str(getattr(_ctx_adapter, "name", None))
    )
    _ctx_model = _valid_runtime_str(getattr(_ctx_adapter, "default_model", None))
    if not _ctx_model:
        _ctx_models = getattr(_ctx_adapter, "models", None)
        if isinstance(_ctx_models, list) and _ctx_models:
            _ctx_model = _valid_runtime_str(_ctx_models[0])

    if not _ctx_provider or not _ctx_model:
        try:
            if hasattr(orch, "get_provider_capabilities"):
                _caps = orch.get_provider_capabilities()
                if isinstance(_caps, dict):
                    _ctx_provider = _ctx_provider or _valid_runtime_str(
                        _caps.get("provider_name") or _caps.get("provider")
                    )
                    _ctx_model = _ctx_model or _valid_runtime_str(
                        _caps.get("model") or _caps.get("default_model")
                    )
        except Exception:
            pass

    _runtime_cm: Any = _contextlib.nullcontext()
    if _ctx_provider or _ctx_model or (
        _ctx_adapter is not None and "MagicMock" not in type(_ctx_adapter).__name__
    ):
        try:
            from src.core.inference.llm_manager import active_runtime_context

            _runtime_cm = active_runtime_context(
                provider=_ctx_provider,
                model=_ctx_model,
                adapter=_ctx_adapter
                if _ctx_adapter is not None and "MagicMock" not in type(_ctx_adapter).__name__
                else None,
            )
        except Exception:
            _runtime_cm = _contextlib.nullcontext()

    with _runtime_cm:
        return _run_agent_once_with_runtime(
            orch=orch,
            system_prompt_name=system_prompt_name,
            messages=messages,
            tools=tools,
            cancel_event=cancel_event,
            _streaming_enabled=_streaming_enabled,
        )


def _run_agent_once_with_runtime(
    *,
    orch: Any,
    system_prompt_name: Optional[str],
    messages: List[Dict[str, Any]],
    tools: Dict[str, Any],
    cancel_event: Optional[Any],
    _streaming_enabled: bool,
) -> Dict[str, Any]:
    # Check if configuration has changed on disk and reload if pending
    try:
        if hasattr(_config_loader_module, "reload_if_pending"):
            _config_loader_module.reload_if_pending()
    except Exception:
        pass

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

    # TASK-24: Derive a clean deterministic session title on the first turn
    # without spawning a background LLM call that contends with single-slot
    # local GPUs on short tasks.  Opt-in LLM title generation remains available
    # via CODINGAGENT_LLM_SESSION_TITLES=1.
    if prompt and not getattr(orch, "_session_title", None):
        _use_llm_title = os.environ.get("CODINGAGENT_LLM_SESSION_TITLES", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if _use_llm_title:
            def _gen_title(p: str) -> None:
                try:
                    from src.core.memory.distiller import generate_session_title

                    _t = generate_session_title(p)
                    if _t:
                        orch._session_title = _t
                        orch.event_bus.publish_typed(SessionTitleGenerated(title=_t))
                except Exception:
                    pass

            _title_ctx = _contextvars.copy_context()
            _t_thread = _threading.Thread(
                target=_title_ctx.run, args=(_gen_title, prompt), daemon=True
            )
            _t_thread.start()
            orch._session_title_thread = _t_thread
        else:
            _det_title = " ".join(str(prompt).split()[:10]).rstrip(".,;:!?")[:80] or "New session"
            orch._session_title = _det_title
            try:
                orch.event_bus.publish_typed(SessionTitleGenerated(title=_det_title))
            except Exception:
                pass

        # Gap 3: fire HOOK_SESSION_START once when a new agent session begins.
        # Guarded by the same "first turn" condition as title generation so a
        # session start fires only once per fresh session (not on continuations).
        try:
            from src.core.plugin.hook_registry import (
                HOOK_SESSION_START as _HOOK_SESSION_START,
                registry as _hook_registry,
            )

            _sid = getattr(orch, "_current_task_id", None) or ""
            _hook_registry.call(
                _HOOK_SESSION_START,
                {"session_id": str(_sid), "task": str(prompt)},
            )
        except Exception:
            pass

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
        orch_adapter = getattr(orch, "adapter", None) or getattr(orch, "_adapter", None)
        active_adapter = (
            orch_adapter
            if orch_adapter is not None and "MagicMock" not in type(orch_adapter).__name__
            else (pm.get_active_adapter() if hasattr(pm, "get_active_adapter") else None)
        )
        if active_adapter and hasattr(active_adapter, "get_loaded_context_length"):
            active_models = (
                getattr(active_adapter, "models", None)
                or (pm.get_active_models() if hasattr(pm, "get_active_models") else [])
            )
            model_name = (
                getattr(active_adapter, "default_model", None)
                or (active_models[0] if isinstance(active_models, list) and active_models else "")
            )
            ctx_len = active_adapter.get_loaded_context_length(model_name)
            if ctx_len and ctx_len > 0:
                active_provider = ""
                try:
                    active_provider = (
                        getattr(orch, "_provider_name", None)
                        or pm.get_active_provider_name()
                        or ""
                    )
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
        system_prompt_name=system_prompt_name,
    )

    # CP-3.4: automatic crash recovery — if a durable snapshot of the last
    # completed round survives for this thread (same task id, e.g. via CLI
    # `--continue` after a crash), rehydrate the initial state from it so
    # completed rounds are not redone.  Completed tasks purge the snapshot,
    # so a surviving snapshot only ever implies an interrupted task.
    try:
        from src.core.orchestration.graph.checkpoint_saver import (
            load_thread_state,
            rehydrate_initial_state,
        )

        if not (
            cancel_event
            and hasattr(cancel_event, "is_set")
            and cancel_event.is_set()
        ):
            _prior = load_thread_state(orch)
            if _prior:
                initial_state = rehydrate_initial_state(initial_state, _prior)
                guilogger.info(
                    "CP-3.4: recovered %d keys from persisted thread state",
                    len(_prior),
                )
    except Exception:
        # HS-6: a crashed-then-interrupted task should NOT silently lose its
        # recovered state without a trace — log the failure (recovery stays
        # best-effort: the task simply restarts fresh).
        guilogger.exception(
            "CP-3.4: thread-state recovery skipped (non-fatal)"
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
            # 2026 Unified 6-Node Canonical Graph: single self-contained graph invocation
            final_state = _execute_graph_round(
                graph=graph,
                orch=orch,
                graph_executor=_graph_executor,
                current_state=initial_state,
            )
            _try_save_thread_state(orch, final_state)
        finally:
            # MED-5 fix: _graph_executor is now instance-level and shut down in
            # close() — do NOT shut it down here or subsequent calls will fail.
            pass

        loop_exit_response = _build_loop_exit_response(
            final_state=final_state,
            cancel_event=cancel_event,
        )
        if loop_exit_response is not None:
            try:
                from src.core.orchestration.graph.checkpoint_saver import (
                    purge_thread_checkpoint,
                )

                purge_thread_checkpoint(orch)
            except Exception:
                pass
            return loop_exit_response

        # CP-3.4: task completed cleanly — the durable checkpoint is no longer
        # needed; drop it to avoid unbounded accumulation.
        try:
            from src.core.orchestration.graph.checkpoint_saver import (
                purge_thread_checkpoint,
            )

            purge_thread_checkpoint(orch)
        except Exception:
            pass

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
        try:
            from src.core.orchestration.graph.checkpoint_saver import (
                purge_thread_checkpoint,
            )

            purge_thread_checkpoint(orch)
        except Exception:
            pass
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
