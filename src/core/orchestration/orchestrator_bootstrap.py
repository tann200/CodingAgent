"""Orchestrator subsystem bootstrap (Phase F refactor).

``bootstrap_orchestrator(orch, message_max_tokens)`` contains the full body of
``Orchestrator.__init__`` after the basic attribute assignments, with all
``self.X`` references mechanically replaced with ``orch.X``.

The Orchestrator.__init__ delegates to this function so the constructor stays
small and each subsystem can be tested in isolation.

ARCH-VOL21-2: the original monolithic 446-line ``bootstrap_orchestrator`` has
been split into four focused private helpers:

  _init_infrastructure   — MessageManager, thread pools, working dir,
                            RollbackManager, FileLockManager, GitSnapshotManager,
                            SessionStore, LifecycleManager, SessionManager.
  _init_providers        — provider/adapter selection, startup event publish.
  _init_event_subscriptions — all event-bus subscriptions (provider, session,
                              permission, bash-approval events).
  _init_services         — TokenBudgetMonitor, ContextController, PreviewService,
                            PlanMode, CostTracker, ToolExecutionService, MCP server.

``bootstrap_orchestrator`` itself is now ~25 lines — it just calls the four
helpers in order.
"""

from __future__ import annotations

import concurrent.futures as _cf
import threading as _threading
import time
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING, cast

from src.core.logger import logger as guilogger
import weakref
from src.core.orchestration.approval_gate import (
    resolve_bash_gate,
    resolve_tool_gate,
)
from src.core.orchestration.message_manager import MessageManager
from src.core.inference.llm_manager import (
    get_provider_manager,
    _ensure_provider_manager_initialized_sync,
)


# ---------------------------------------------------------------------------
# Phase 1 — Core infrastructure
# ---------------------------------------------------------------------------


def _init_infrastructure(orch: Any, message_max_tokens: Optional[int]) -> None:
    """Wire MessageManager, thread pools, working directory, and all managers."""
    # Wire the MessageManager with compaction support so dropped messages
    # are summarised inline rather than silently discarded.
    orch.msg_mgr = MessageManager(
        max_tokens=message_max_tokens,
        event_bus=orch.event_bus,
        compact_callback=orch._compact_messages,
    )
    # Lightweight lock to protect in-memory message replacement during
    # background compaction operations. Present but optional for tests that
    # create fake orchestrators.
    try:
        orch._msg_mgr_lock = _threading.Lock()
    except Exception:
        orch._msg_mgr_lock = None

    orch._max_files_per_task = 10
    # F17: In-memory usage counter — flushed once per run_agent_once() instead of per tool call
    orch._usage_buffer = {}  # type: ignore[attr-defined]

    # HR-3 fix: create a single reusable ThreadPoolExecutor for tool timeouts.
    orch._tool_executor = _cf.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="tool_timeout"
    )

    # MED-5 fix: create a single reusable ThreadPoolExecutor for graph execution.
    orch._graph_executor = _cf.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="graph_exec"
    )

    # Default working directory logic

    repo_root = Path(__file__).parents[3]
    default_out = repo_root / "output"
    try:
        cwd = Path.cwd().resolve()
        repo_root_resolved = repo_root.resolve()
        if str(cwd).startswith(str(repo_root_resolved)) or str(
            repo_root_resolved
        ) in str(cwd):
            default_wd = default_out
        else:
            default_wd = cwd
    except Exception:
        default_wd = default_out
    orch.working_dir = orch.working_dir if orch.working_dir is not None else default_wd
    orch._allow_external = bool(orch._allow_external)
    orch._ensure_working_dir()

    # PERF-1: Evict any stale repo summary for this working directory.
    try:
        from src.core.orchestration.graph.nodes.analysis_node import (
            clear_repo_summary_cache as _clear_rsc,
        )

        _clear_rsc(str(orch.working_dir))
    except Exception:
        pass

    # Initialize RollbackManager for automated rollback on failure
    from src.core.orchestration.rollback_manager import RollbackManager

    orch.rollback_manager = RollbackManager(str(orch.working_dir))
    orch._current_snapshot_id = None  # type: ignore[attr-defined]
    orch._step_snapshot_id = None  # type: ignore[attr-defined]

    # Initialize FileLockManager for PRSW (Parallel Reads, Sequential Writes)
    from src.core.orchestration.file_lock_manager import FileLockManager

    orch.file_lock_manager = FileLockManager(
        workdir=str(orch.working_dir),
        cancel_event=getattr(orch, "cancel_event", None),
    )

    # S4-A: Initialize GitSnapshotManager for workspace snapshots.
    from src.core.orchestration.snapshot_manager import GitSnapshotManager

    _project_id = orch.working_dir.name or "default"
    orch.snapshot_manager = GitSnapshotManager(
        workspace=orch.working_dir,
        project_id=_project_id,
        enabled=True,
    )

    # Initialize SessionStore. Use the raw backend factory to prefer returning
    # the concrete implementation (SqliteSessionStore or JsonlSessionStore).
    # This satisfies tests that assert the orchestrator has a raw backend
    # instance (not the compatibility wrapper). Fall back to the factory
    # if raw creation fails.
    try:
        from src.core.memory.session_store import _create_backend as _raw_create

        orch.session_store = _raw_create(str(orch.working_dir))
    except Exception:
        try:
            from src.core.memory.session_store import get_session_store

            orch.session_store = get_session_store(workdir=str(orch.working_dir))
        except Exception:
            # Fallback: instantiate JsonlSessionStore directly if factory fails
            try:
                from src.core.memory.jsonl_session_store import JsonlSessionStore

                orch.session_store = JsonlSessionStore(str(orch.working_dir))
            except Exception:
                orch.session_store = None

    # Initialize SessionLifecycleManager for graceful shutdown and snapshots
    from src.core.orchestration.session_lifecycle import get_session_lifecycle_manager

    orch.lifecycle_manager = get_session_lifecycle_manager(str(orch.working_dir))

    # Register lifecycle shutdown hook for cleanup
    def _lifecycle_cleanup_hook(session_id: str) -> None:
        try:
            guilogger.debug(f"Lifecycle cleanup for session: {session_id}")
            # Best-effort: attempt to close the session store to release DB
            # resources (writer connection). Any failure must not raise.
            try:
                ss = getattr(orch, "session_store", None)
                if ss is not None:
                    close_fn = getattr(ss, "close", None)
                    if callable(close_fn):
                        try:
                            close_fn()
                        except Exception as e:
                            # Log the exception and stacktrace at DEBUG level without
                            # using exc_info to satisfy linters. Use a local import so
                            # we avoid adding module-level dependencies.
                            import traceback

                            guilogger.debug(
                                "Orchestrator: session_store.close() failed during lifecycle cleanup: %s\n%s",
                                e,
                                traceback.format_exc(),
                            )
            except Exception as e:
                # Swallow any unexpected error during cleanup; do not escape.
                # Log debug output including the traceback via traceback.format_exc().
                try:
                    import traceback

                    guilogger.debug(
                        "Orchestrator: failed to invoke session_store.close(): %s\n%s",
                        e,
                        traceback.format_exc(),
                    )
                except Exception:
                    pass
        except Exception as e:
            guilogger.warning(f"Lifecycle cleanup hook failed: {e}")

    orch.lifecycle_manager.on_shutdown("session_store_flush", _lifecycle_cleanup_hook)

    # Subscribe to task completion events for automatic snapshot.
    def _on_task_complete(payload: Any) -> None:
        orch._create_session_snapshot()

    orch.event_bus.subscribe("task.completed", _on_task_complete)

    # Subscribe to task failure for snapshot before cleanup
    def _on_task_failed(payload: Any) -> None:
        if payload.get("session_id") == orch._current_task_id:
            orch._create_session_snapshot()

    orch.event_bus.subscribe("task.failed", _on_task_failed)

    # ARCH-1: Instantiate SessionManager
    from src.core.orchestration.session_manager import SessionManager as _SM

    if TYPE_CHECKING:
        # Import only for type-checkers to recognise the SessionStore type; avoid a
        # runtime import cycle by keeping this inside TYPE_CHECKING.
        from src.core.memory.session_store import SessionStore  # pragma: no cover

    # orch.session_store may be either JsonlSessionStore or SessionStore. For
    # type-checkers this is a Union; cast to the expected SessionStore to
    # silence pyright while preserving runtime behaviour.
    orch.session_mgr = _SM(
        working_dir=orch.working_dir,
        session_store=cast("SessionStore", orch.session_store),
        lifecycle_manager=orch.lifecycle_manager,
        event_bus=orch.event_bus,
    )
    # msg_mgr is set just above; wire it into session_mgr now.
    orch.session_mgr.msg_mgr = orch.msg_mgr


# ---------------------------------------------------------------------------
# Phase 2 — Provider / adapter wiring
# ---------------------------------------------------------------------------


def _init_providers(orch: Any) -> None:
    """Select and activate the LLM provider adapter."""
    pm = None
    try:
        pm = get_provider_manager()
        if pm:
            _wire_provider_manager(orch, pm)
            _select_default_adapter(orch, pm)
            # HANG-FIX: _publish_active_config must be called explicitly when _adapter
            # is set directly (not through the adapter property setter) so that
            # model_tier and other capability-derived fields are populated before
            # graph nodes call call_model with provider=None model=None.
            if orch._adapter is not None:
                try:
                    from src.core.orchestration.orchestrator_helpers import (
                        _publish_active_config_impl,
                    )

                    _publish_active_config_impl(orch)
                except Exception:
                    pass
    except Exception:
        pass

    _publish_startup_events(orch, pm)


def _wire_provider_manager(orch: Any, pm: Any) -> None:
    """Attach the orchestrator event bus to the provider manager and initialize it."""
    if getattr(pm, "_event_bus", None) is None:
        pm.set_event_bus(orch.event_bus)
    else:
        orch.event_bus = getattr(pm, "_event_bus")
    _ensure_provider_manager_initialized_sync()


def _select_default_adapter(orch: Any, pm: Any) -> None:
    """Pick the default adapter when the orchestrator has not been given one."""
    if orch._adapter is not None:
        return

    providers = pm.list_providers()
    guilogger.info(f"Orchestrator init: available providers: {providers}")
    if not providers:
        return

    active_name = None
    try:
        active_name = pm.get_active_provider_name()
    except Exception:
        pass

    if active_name and active_name in providers:
        name = active_name
    elif "lm_studio" in providers:
        name = "lm_studio"
    else:
        name = providers[0]

    orch._adapter = pm.get_provider(name)
    guilogger.info(
        f"Orchestrator init: picked adapter: {name}, adapter: {orch._adapter}"
    )


def _publish_startup_events(orch: Any, pm: Any) -> None:
    """Publish orchestrator startup events to the active event buses."""
    try:
        payload = {"time": time.time(), "working_dir": str(orch.working_dir)}
        try:
            guilogger.info("Orchestrator: publishing startup to orch.event_bus")
            orch.event_bus.publish("orchestrator.startup", payload)
        except Exception:
            pass

        try:
            pm_bus = getattr(pm, "_event_bus", None)
            if pm_bus and pm_bus is not orch.event_bus:
                guilogger.info("Orchestrator: publishing startup to pm_bus")
                pm_bus.publish("orchestrator.startup", payload)
        except Exception:
            pass
    except Exception:
        pass


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
                    "session_id": session_id
                    or getattr(orch, "_current_task_id", "default"),
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
        # Run distillation in a background thread so the EventBus publisher
        # is non-blocking. The handler must fail softly.
        try:
            import threading as _threading

            def _worker() -> None:
                try:
                    from src.core.memory.distiller import distill_context

                    # Use orch.msg_mgr.messages as the source of truth; copy to
                    # avoid concurrent mutation while distillation runs.
                    msgs = []
                    try:
                        msgs = list(getattr(orch, "msg_mgr").messages or [])
                    except Exception:
                        msgs = []
                    # Call distill_context with the orchestrator working_dir.
                    try:
                        # Capture the distillation result so we can apply any
                        # returned compacted history back into the in-memory
                        # MessageManager.  This keeps the handler fail-soft.
                        distilled = distill_context(
                            msgs, working_dir=getattr(orch, "working_dir", None)
                        )

                        # If the distiller returned a compacted history, apply it
                        # to the orchestrator's MessageManager so the in-memory
                        # context actually shrinks.
                        try:
                            if isinstance(distilled, dict) and (
                                "_compacted_history" in distilled
                            ):
                                compacted = distilled.get("_compacted_history")
                                if compacted is not None:
                                    try:
                                        if (
                                            hasattr(orch, "msg_mgr")
                                            and getattr(orch, "msg_mgr", None)
                                            is not None
                                        ):
                                            # Prefer acquiring an orchestrator-owned
                                            # lock if available to avoid races with
                                            # concurrent message appends.
                                            lock = getattr(orch, "_msg_mgr_lock", None)
                                            try:
                                                if lock:
                                                    with lock:
                                                        orch.msg_mgr.messages = list(
                                                            compacted
                                                        )
                                                else:
                                                    orch.msg_mgr.messages = list(
                                                        compacted
                                                    )
                                            except Exception:
                                                # Swallow assignment errors; never raise
                                                pass

                                            # Compute simple token metrics (best-effort)
                                            _orig_tok = None
                                            _new_tok = None
                                            _tokens_reduced = None
                                            try:
                                                from src.core.memory import (
                                                    distiller as _distiller,
                                                )

                                                try:
                                                    _orig_tok = (
                                                        _distiller._estimate_tokens(
                                                            msgs or []
                                                        )
                                                    )
                                                except Exception:
                                                    _orig_tok = None
                                                try:
                                                    _new_tok = (
                                                        _distiller._estimate_tokens(
                                                            compacted or []
                                                        )
                                                    )
                                                except Exception:
                                                    _new_tok = None
                                                if (
                                                    _orig_tok is not None
                                                    and _new_tok is not None
                                                ):
                                                    _tokens_reduced = (
                                                        _orig_tok - _new_tok
                                                    )
                                            except Exception:
                                                _orig_tok = _new_tok = (
                                                    _tokens_reduced
                                                ) = None

                                            # Publish a compact-applied event for observability
                                            try:
                                                orch.event_bus.publish(
                                                    "message.compaction_applied",
                                                    {
                                                        "source": "scheduler",
                                                        "original_count": len(msgs),
                                                        "new_count": len(compacted),
                                                        "dropped_count": max(
                                                            0,
                                                            len(msgs) - len(compacted),
                                                        ),
                                                        "original_tokens": _orig_tok,
                                                        "new_tokens": _new_tok,
                                                        "tokens_reduced": _tokens_reduced,
                                                    },
                                                )
                                            except Exception:
                                                pass
                                    except Exception:
                                        # Assignment must not raise to the EventBus
                                        pass
                        except Exception:
                            pass

                        # Publish completion event for observability.
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
                    # Any unexpected error must not escape silently.
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
        orch.event_bus.subscribe(
            "scheduler.distill_request", _on_scheduler_distill_request
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 3 — Event-bus subscriptions
# ---------------------------------------------------------------------------


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

    # GAP-PERM-2: route denial feedback to the agent so it knows why it was denied
    def _on_denial_feedback(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        feedback = payload.get("feedback", "")
        tool_id = payload.get("tool_id", "")
        if feedback and tool_id:
            # Append feedback as a system message so the agent sees why it was denied
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


# ---------------------------------------------------------------------------
# Phase 4 — Service layer
# ---------------------------------------------------------------------------


def _init_services(orch: Any) -> None:
    """Initialise token monitor, preview service, plan mode, cost tracker, and tool service."""
    # async check in background
    try:
        _threading.Thread(target=orch._background_model_check, daemon=True).start()
    except Exception:
        try:
            orch._background_model_check()
        except Exception:
            pass

    # Initial publish of current config
    orch._publish_active_config()

    # Phase 4: Initialize Token Budget Monitor and Context Controller
    from src.core.orchestration.token_budget import get_token_budget_monitor

    orch.token_monitor = get_token_budget_monitor()

    from src.core.context.context_controller import get_context_controller

    orch.context_controller = get_context_controller(
        max_tokens=getattr(orch, "_message_max_tokens", None) or 6000
    )

    # Phase 3: Initialize Preview Service for diff previews
    from src.core.orchestration.preview_service import get_preview_service

    orch.preview_service = get_preview_service(str(orch.working_dir))
    orch._pending_preview_id = None  # type: ignore[attr-defined]

    # Plan Mode: blocks write tools until user approves the plan
    from src.core.orchestration.plan_mode import PlanMode

    orch.plan_mode = PlanMode(orchestrator=orch)
    orch._plan_approval_event = None  # type: ignore[attr-defined]
    orch._plan_approved = False  # type: ignore[attr-defined]

    # Explore Mode
    orch.explore_mode = False  # type: ignore[attr-defined]

    # Role management
    orch.current_role = None  # type: ignore[attr-defined]
    orch.role_manager = None  # type: ignore[attr-defined]

    # Permission gate state (subscriptions registered in _init_event_subscriptions)
    orch._permission_gate = None  # type: ignore[attr-defined]
    orch._permission_granted = False  # type: ignore[attr-defined]

    # D-10: TUI-05 diff-preview subscriptions now delegated to PreviewCoordinator.
    from src.core.orchestration.preview_coordinator import PreviewCoordinator

    orch.preview_coordinator = PreviewCoordinator()
    try:
        orch.preview_coordinator.attach(orch.event_bus)
    except Exception:
        pass

    # D-10: SessionCostTracker
    from src.core.orchestration.session_cost_tracker import SessionCostTracker
    from src.core.orchestration.project_settings import get_active_settings as _get_ps

    _active_ps = _get_ps()
    _budget_ceiling = _active_ps.budget_ceiling_usd if _active_ps is not None else None
    orch.cost_tracker = SessionCostTracker(
        working_dir=orch.working_dir,
        event_bus=orch.event_bus,
        budget_ceiling_usd=_budget_ceiling,
    )

    # D-10: ToolExecutionService
    from src.core.orchestration.tool_execution_service import ToolExecutionService

    _hook_runner = None
    try:
        from src.core.orchestration.tool_hooks import ToolHookRunner

        _hook_runner = ToolHookRunner()
    except Exception:
        pass
    orch.tool_execution_service = ToolExecutionService(
        registry=orch.tool_registry,
        event_bus=orch.event_bus,
        hook_runner=_hook_runner,
    )

    # MCP STDIO server (Step 9): instantiated but not started by default.
    orch._mcp_server = None

    # Gap 1: outbound MCP multi-server manager (config-driven).
    try:
        from src.core.mcp.manager import McpServerManager

        orch._mcp_manager = McpServerManager(
            registry=orch.tool_registry,
            event_bus=orch.event_bus,
            working_dir=orch.working_dir,
        )
        # Public alias consumed by deferred_init (trusted-gated startup).
        orch.mcp_manager = orch._mcp_manager
    except Exception:
        orch._mcp_manager = None
        orch.mcp_manager = None

    # Gap 2: HTTP/SSE server for multi-client architecture
    try:
        from src.server.app import ServerEventBusAdapter, run_server
        import threading

        # Only start server if enabled via environment variable or config
        import os

        if os.getenv("CODING_AGENT_HTTP_SERVER", "false").lower() == "true":
            # Import the server module to set global instances
            import src.server.app as server_app

            # Register the orchestrator EventBus with the server module so
            # the server can attach its internal subscribers (metrics, SSE
            # adapter) rather than assigning globals directly.
            try:
                server_app.register_event_bus(orch.event_bus)
            except Exception:
                # Fallback: preserve previous behaviour in case register_event_bus
                # is unavailable for some environment or packaging reason.
                server_app.event_bus = orch.event_bus
                server_app.sse_adapter = ServerEventBusAdapter(orch.event_bus)

            # Start server in a background thread
            server_thread = threading.Thread(
                target=lambda: run_server(host="127.0.0.1", port=8000), daemon=True
            )
            server_thread.start()
            orch._http_server_thread = server_thread
            guilogger.info("HTTP/SSE server started on http://127.0.0.1:8000")
        else:
            orch._http_server_thread = None
    except Exception as e:
        guilogger.warning(f"Failed to start HTTP/SSE server: {e}")
        orch._http_server_thread = None


# ---------------------------------------------------------------------------
# Config reload helper
# ---------------------------------------------------------------------------


def register_config_reload_handlers(orch: Any) -> None:
    """Register a callback with the ConfigReloader to refresh runtime state.

    This function is intentionally safe to call in tests and avoids heavy
    imports until the callback actually runs.
    """
    # Avoid registering the same callback multiple times for the same
    # orchestrator instance. Use a weak set to avoid leaking memory when
    # orchestrator instances are garbage collected.
    try:
        if not hasattr(register_config_reload_handlers, "_registered"):
            register_config_reload_handlers._registered = weakref.WeakSet()
        if orch in register_config_reload_handlers._registered:
            return
        register_config_reload_handlers._registered.add(orch)
    except Exception:
        # If weakref operations fail for any reason, proceed (best-effort).
        pass

    try:
        from src.core.config_hot_reload import get_config_reloader

        def _on_config_reloaded(changed_paths: Optional[set]) -> None:
            guilogger.info("Orchestrator: config reloader callback invoked")
            # 1) Reload AgentBrainManager caches
            try:
                from src.core.orchestration.agent_brain import get_agent_brain_manager

                try:
                    get_agent_brain_manager().reload()
                except Exception:
                    guilogger.warning(
                        "AgentBrainManager.reload failed during config reload"
                    )
            except Exception:
                guilogger.debug("AgentBrainManager not available to reload")

            # 2) Clear toolset loader cache so updated YAMLs are re-read
            try:
                from src.config.toolsets import loader as _ts_loader

                try:
                    _ts_loader.clear_cache()
                except Exception:
                    guilogger.warning(
                        "toolsets.loader.clear_cache failed during config reload"
                    )
            except Exception:
                guilogger.debug("toolsets.loader not available to clear cache")

            # 3) Rebuild the tool registry and replace orch.tool_registry
            try:
                from src.core.orchestration.registry_builder import example_registry

                try:
                    new_reg = example_registry()
                    if new_reg:
                        orch.tool_registry = new_reg
                        guilogger.info(
                            "Orchestrator: tool registry rebuilt from config reload"
                        )
                except Exception:
                    guilogger.warning(
                        "Failed to rebuild tool registry during config reload"
                    )
            except Exception:
                guilogger.debug("registry_builder.example_registry not available")

            # 4) Reinitialize providers so provider changes (providers.json) take effect
            try:
                pm = None
                from src.core.inference.llm_manager import (
                    get_provider_manager,
                    _ensure_provider_manager_initialized_sync,
                )

                try:
                    pm = get_provider_manager()
                    if pm:
                        if getattr(pm, "_event_bus", None) is None:
                            pm.set_event_bus(orch.event_bus)
                        _ensure_provider_manager_initialized_sync()
                except Exception:
                    guilogger.warning(
                        "ProviderManager reinitialization failed during config reload"
                    )
            except Exception:
                guilogger.debug("llm_manager ProviderManager not available for reload")

            # 5) Publish a top-level event so UIs or other subsystems can react.
            try:
                orch.event_bus.publish(
                    "config.reloaded", {"changed_paths": list(changed_paths or [])}
                )
            except Exception:
                guilogger.debug(
                    "Failed to publish orchestrator-level config.reloaded event"
                )

            # 6) Restart scheduler to pick up config changes safely
            try:
                # Stop existing scheduler (best-effort) then re-initialize.
                try:
                    _sched = getattr(orch, "_scheduler", None)
                    if _sched is not None:
                        try:
                            _sched.stop_scheduler()
                        except Exception:
                            pass
                        try:
                            # Clear any registered jobs so re-init starts clean.
                            if hasattr(_sched, "clear_jobs"):
                                _sched.clear_jobs()
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    # Re-run scheduler init helper to pick up env/config changes
                    try:
                        _init_scheduler(orch)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                pass

        try:
            get_config_reloader(initial_load=False).add_callback(_on_config_reloaded)
        except Exception:
            guilogger.debug("Failed to register config reloader callback")
    except Exception:
        # No config reloader available; skip registration silently
        pass


def _init_scheduler(orch: Any) -> None:
    """Initialize and start the lightweight scheduler for the orchestrator.

    Kept as a small, standalone helper so bootstrap_orchestrator() remains
    a thin wrapper (ARCH-VOL21-2). This function is conservative and fails
    silently on any error to avoid impacting startup.
    """
    try:
        import os as _os

        from src.core.scheduler import worker as _sched

        _hb = int(_os.getenv("CODING_AGENT_SCHEDULER_HEARTBEAT", "60") or 60)
        _dist_int = int(_os.getenv("CODING_AGENT_DISTILL_INTERVAL", "600") or 600)

        def _publish_distill_request() -> None:
            try:
                orch.event_bus.publish(
                    "scheduler.distill_request",
                    {"source": "scheduler", "time": time.time()},
                )
            except Exception:
                try:
                    guilogger.warning("Scheduler: failed to publish distill_request")
                except Exception:
                    pass

        # Allow per-job config via merged config layer: "scheduler_jobs".
        # Example: {"periodic_distill_request": {"enabled": true, "interval": 600}}
        try:
            from src.core.config_loader import get as _cfg_get

            _sched_cfg = _cfg_get("scheduler_jobs", {}) or {}
        except Exception:
            _sched_cfg = {}

        _pd_cfg = (
            _sched_cfg.get("periodic_distill_request", {})
            if isinstance(_sched_cfg, dict)
            else {}
        )
        _pd_enabled = _pd_cfg.get("enabled", True)
        _pd_interval = int(_pd_cfg.get("interval", _dist_int))

        if _pd_enabled:
            _sched.register_job(
                "periodic_distill_request", _publish_distill_request, _pd_interval
            )
        _sched.start_scheduler(orch, heartbeat_interval=_hb)
        orch._scheduler = _sched
        try:
            orch.lifecycle_manager.on_shutdown(
                "stop_scheduler", lambda _sid: _sched.stop_scheduler()
            )
        except Exception:
            pass
    except Exception:
        # Never fail orchestrator bootstrap because scheduler init failed.
        pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def bootstrap_orchestrator(orch: Any, message_max_tokens: Optional[int]) -> None:
    """Wire all subsystems onto orch.  Called from Orchestrator.__init__ after
    basic scalar attributes (_dry_run, _adapter, _provider_name, tool_registry,
    event_bus) have been set.

    Delegates to four focused helpers (ARCH-VOL21-2):
      1. _init_infrastructure      — managers, executors, working dir
      2. _init_providers           — adapter selection, startup events
      3. _init_event_subscriptions — all event-bus subscribers
      4. _init_services            — token monitor, preview, plan mode, tools
    """
    # Stash max_tokens so _init_services can read it without a parameter chain.
    orch._message_max_tokens = message_max_tokens  # type: ignore[attr-defined]

    _init_infrastructure(orch, message_max_tokens)
    _init_providers(orch)
    _init_event_subscriptions(orch)
    _init_services(orch)

    # Register configuration hot-reload callback helper. We already expose a
    # module-level register_config_reload_handlers() above; attach that helper
    # to the orchestrator instance and invoke it. Keeping the heavy logic at
    # module scope keeps bootstrap_orchestrator short (ARCH-VOL21-2 requirement)
    orch.register_config_reload_handlers = register_config_reload_handlers
    try:
        register_config_reload_handlers(orch)
    except Exception:
        guilogger.debug("Failed to register config reload handlers at bootstrap")

    # Gap 8: Bridge EventBus topics to OTel span events (no-op when OTel is disabled).
    try:
        from src.core.telemetry.tracer import wire_event_bus as _wire_otel

        _wire_otel(orch.event_bus)
    except Exception:
        pass

    # Start the scheduler via a small helper so bootstrap_orchestrator remains thin
    try:
        _init_scheduler(orch)
    except Exception:
        pass
