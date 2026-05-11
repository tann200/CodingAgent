"""Event-bus subscription helpers for the orchestrator bootstrap."""

from __future__ import annotations

from typing import Any

from src.core.logger import logger as guilogger
from src.core.orchestration.approval_gate import resolve_bash_gate, resolve_tool_gate


def _register_provider_event_subscriptions(orch: Any) -> None:
    """Register provider-related event subscriptions."""

    def _on_provider_config_missing(payload: Any) -> None:
        guilogger.warning(f"Orchestrator detected missing provider config: {payload}")
        try:
            orch.event_bus.publish(
                "ui.notification",
                {
                    "level": "error",
                    "message": "No provider configured. Open settings to connect LM Studio or Ollama.",
                },
            )
        except Exception:
            pass

    def _on_provider_status_changed(payload: Any) -> None:
        guilogger.info(f"Orchestrator: provider status changed: {payload}")
        try:
            if isinstance(payload, dict) and payload.get("status") == "disconnected":
                orch.event_bus.publish(
                    "ui.notification",
                    {
                        "level": "warning",
                        "message": f"Provider {payload.get('provider')} is disconnected.",
                    },
                )
        except Exception:
            pass

    def _on_provider_model_missing(payload: Any) -> None:
        guilogger.warning(f"Provider model missing: {payload}")
        try:
            if isinstance(payload, dict):
                orch.event_bus.publish(
                    "ui.notification",
                    {
                        "level": "warning",
                        "message": f"Model {payload.get('requested')} missing on provider {payload.get('provider')}",
                    },
                )
        except Exception:
            pass

    try:
        orch.event_bus.subscribe("provider.config.missing", _on_provider_config_missing)
        orch.event_bus.subscribe("provider.status.changed", _on_provider_status_changed)
        orch.event_bus.subscribe("provider.model.missing", _on_provider_model_missing)
    except Exception:
        pass

    def _on_models_probing_started(payload: Any) -> None:
        guilogger.info(f"Orchestrator: provider models probing started: {payload}")
        try:
            orch.event_bus.publish("orchestrator.models.check.started", payload)
        except Exception:
            pass

    def _on_models_probing_completed(payload: Any) -> None:
        guilogger.info(f"Orchestrator: provider models probing completed: {payload}")
        try:
            orch.event_bus.publish("orchestrator.models.check.completed", payload)
        except Exception:
            pass

    def _on_models_probing_failed(payload: Any) -> None:
        guilogger.error(f"Orchestrator: provider models probing failed: {payload}")
        try:
            orch.event_bus.publish("orchestrator.models.check.failed", payload)
        except Exception:
            pass

    try:
        orch.event_bus.subscribe(
            "provider.models.probing_started", _on_models_probing_started
        )
        orch.event_bus.subscribe(
            "provider.models.probing_completed", _on_models_probing_completed
        )
        orch.event_bus.subscribe(
            "provider.models.probing_failed", _on_models_probing_failed
        )
    except Exception:
        pass


def _register_session_hydration_subscription(orch: Any) -> None:
    """Register the session state hydration subscription."""

    def _on_session_request_state(payload: Any) -> None:
        try:
            session_id = payload.get("session_id") if isinstance(payload, dict) else None
            history = []
            try:
                if hasattr(orch, "msg_mgr") and orch.msg_mgr:
                    history = list(orch.msg_mgr.messages or [])
            except Exception:
                pass
            orch.event_bus.publish(
                "session.hydrated",
                {
                    "session_id": session_id or getattr(orch, "_current_task_id", "default"),
                    "messageHistory": history,
                    "currentTask": getattr(orch, "_current_task", ""),
                    "workingDir": str(orch.working_dir),
                },
            )
        except Exception:
            pass

    try:
        orch.event_bus.subscribe("session.request_state", _on_session_request_state)
    except Exception:
        pass


def _register_scheduler_distill_subscription(orch: Any) -> None:
    """Register the scheduler-triggered background distillation subscription."""

    def _on_scheduler_distill_request(payload: Any) -> None:
        try:
            import threading as _threading

            def _worker() -> None:
                try:
                    from src.core.memory.distiller import distill_context

                    msgs = []
                    try:
                        msgs = list(getattr(orch, "msg_mgr").messages or [])
                    except Exception:
                        msgs = []
                    try:
                        distilled = distill_context(
                            msgs, working_dir=getattr(orch, "working_dir", None)
                        )
                        try:
                            if isinstance(distilled, dict) and (
                                "_compacted_history" in distilled
                            ):
                                compacted = distilled.get("_compacted_history")
                                if compacted is not None:
                                    try:
                                        if (
                                            hasattr(orch, "msg_mgr")
                                            and getattr(orch, "msg_mgr", None) is not None
                                        ):
                                            lock = getattr(orch, "_msg_mgr_lock", None)
                                            try:
                                                if lock:
                                                    with lock:
                                                        orch.msg_mgr.messages = list(compacted)
                                                else:
                                                    orch.msg_mgr.messages = list(compacted)
                                            except Exception:
                                                pass

                                            _orig_tok = None
                                            _new_tok = None
                                            _tokens_reduced = None
                                            try:
                                                from src.core.memory import distiller as _distiller

                                                try:
                                                    _orig_tok = _distiller._estimate_tokens(msgs or [])
                                                except Exception:
                                                    _orig_tok = None
                                                try:
                                                    _new_tok = _distiller._estimate_tokens(compacted or [])
                                                except Exception:
                                                    _new_tok = None
                                                if _orig_tok is not None and _new_tok is not None:
                                                    _tokens_reduced = _orig_tok - _new_tok
                                            except Exception:
                                                _orig_tok = _new_tok = _tokens_reduced = None

                                            try:
                                                orch.event_bus.publish(
                                                    "message.compaction_applied",
                                                    {
                                                        "source": "scheduler",
                                                        "original_count": len(msgs),
                                                        "new_count": len(compacted),
                                                        "dropped_count": max(0, len(msgs) - len(compacted)),
                                                        "original_tokens": _orig_tok,
                                                        "new_tokens": _new_tok,
                                                        "tokens_reduced": _tokens_reduced,
                                                    },
                                                )
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                        try:
                            orch.event_bus.publish(
                                "scheduler.distill_completed", {"source": "scheduler"}
                            )
                        except Exception:
                            pass
                    except Exception as _e:
                        try:
                            guilogger.warning(f"scheduler.distill_request failed: {_e}")
                        except Exception:
                            pass
                except Exception:
                    try:
                        guilogger.warning("scheduler.distill_request unexpected error")
                    except Exception:
                        pass

            _t = _threading.Thread(target=_worker, daemon=True)
            _t.start()
        except Exception:
            try:
                guilogger.warning(
                    "Orchestrator: failed to handle scheduler.distill_request"
                )
            except Exception:
                pass

    try:
        orch.event_bus.subscribe("scheduler.distill_request", _on_scheduler_distill_request)
    except Exception:
        pass


def _register_permission_gate_subscriptions(orch: Any) -> None:
    """Register tool-permission gate event subscriptions."""

    def _on_tool_permission_granted(payload: Any) -> None:
        _tid = str(payload.get("tool_id", "")) if isinstance(payload, dict) else ""
        if _tid:
            resolve_tool_gate(_tid, approved=True)
        gate = orch._permission_gate
        if gate is not None:
            orch._permission_granted = True
            gate.set()

    def _on_tool_permission_denied(payload: Any) -> None:
        _tid = str(payload.get("tool_id", "")) if isinstance(payload, dict) else ""
        if _tid:
            resolve_tool_gate(_tid, approved=False)
        gate = orch._permission_gate
        if gate is not None:
            orch._permission_granted = False
            gate.set()

    def _on_denial_feedback(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        feedback = payload.get("feedback", "")
        tool_id = payload.get("tool_id", "")
        if feedback and tool_id:
            try:
                if hasattr(orch, "state") and "history" in orch.state:
                    orch.state["history"].append(
                        ("system", f"Tool permission denied. Feedback: {feedback}")
                    )
                guilogger.info(
                    f"denial_feedback: added to agent context (tool_id={tool_id})"
                )
            except Exception as e:
                guilogger.warning(f"denial_feedback: failed to add to context: {e}")

    try:
        orch.event_bus.subscribe("tool.permission_granted", _on_tool_permission_granted)
        orch.event_bus.subscribe("tool.permission_denied", _on_tool_permission_denied)
        orch.event_bus.subscribe("tool.denial_feedback", _on_denial_feedback)
    except Exception:
        pass


def _register_bash_approval_subscriptions(orch: Any) -> None:
    """Register bash approval event subscriptions from the TUI."""

    def _on_bash_approval_granted(payload: dict) -> None:
        resolve_bash_gate(str(payload.get("tool_id", "")), approved=True)

    def _on_bash_approval_denied(payload: dict) -> None:
        resolve_bash_gate(str(payload.get("tool_id", "")), approved=False)

    try:
        orch.event_bus.subscribe("bash.approval_granted", _on_bash_approval_granted)
        orch.event_bus.subscribe("bash.approval_denied", _on_bash_approval_denied)
    except Exception:
        pass


def _init_event_subscriptions(orch: Any) -> None:
    """Register all event-bus subscribers (provider, session, permission, bash)."""

    _register_provider_event_subscriptions(orch)
    _register_session_hydration_subscription(orch)
    _register_scheduler_distill_subscription(orch)
    _register_permission_gate_subscriptions(orch)
    _register_bash_approval_subscriptions(orch)
