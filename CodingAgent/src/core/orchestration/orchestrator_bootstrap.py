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

import atexit
import concurrent.futures as _cf
import threading as _threading
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING, cast

from src.core.logger import logger as guilogger
from src.core.orchestration.orchestrator_config_reload import (
    register_config_reload_handlers,
)
from src.core.orchestration.orchestrator_event_subscriptions import (
    _init_event_subscriptions,
)
from src.core.orchestration.orchestrator_provider_init import _init_providers
from src.core.orchestration.orchestrator_scheduler import _init_scheduler
from src.core.orchestration.orchestrator_services_init import _init_services
from src.core.orchestration.message_manager import MessageManager


# ---------------------------------------------------------------------------
# Phase 1 — Core infrastructure
# ---------------------------------------------------------------------------


def _init_infrastructure(orch: Any, message_max_tokens: Optional[int]) -> None:
    """Wire MessageManager, thread pools, working directory, and all managers."""
    # Register executor shutdown for cleanup. Must happen before executor creation
    # so the cleanup handler is wired first.
    def _shutdown_executors() -> None:
        _tool_exe = getattr(orch, "_tool_executor", None)
        if _tool_exe is not None:
            _tool_exe.shutdown(wait=False)
        _graph_exe = getattr(orch, "_graph_executor", None)
        if _graph_exe is not None:
            _graph_exe.shutdown(wait=False)

        atexit.register(_shutdown_executors)

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
    # Uses reset_instance to ensure a clean state on orchestrator re-initialization
    # (e.g., TUI bridge reconnection or hot-reload).
    from src.core.orchestration.file_lock_manager import FileLockManager

    orch.file_lock_manager = FileLockManager.reset_instance(
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
