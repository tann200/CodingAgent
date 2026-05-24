"""
AgentBridge: Connects the Textual UI to the backend EventBus.

When src.core is available, subscribes to the real EventBus obtained via
get_event_bus().  Otherwise falls back to the MockEventBus so mock_engine.py
keeps working in dev mode.

Implements the full threading contract from §9 of the spec:
  _agent_lock, _cancel_event, _history_lock, send_prompt(), interrupt(),
  force_interrupt(), _run_agent(), _schedule_callback().

    History persistence: §15 — atomic JSON at get_data_dir()/tui_conversation_history.json
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from ._core_paths_loader import get_data_dir
from ._bridge_subscriptions import BridgeSubscriptionsMixin
from ._bridge_provider import BridgeProviderMixin
from ._bridge_tools import BridgeToolsMixin
from ._bridge_context import BridgeContextMixin
from ._bridge_session import BridgeSessionMixin
from ._bridge_agent import BridgeAgentMixin

if TYPE_CHECKING:
    from tui.tui_src.ui.app import AgentApp

try:
    from tui.tui_src.ui.logging import get_logger
except Exception:
    try:
        from .logging import get_logger
    except Exception:
        import logging

        def get_logger(name: str) -> logging.Logger:
            return logging.getLogger(name)


logger = get_logger("bridge")


_AGENT_DIR = get_data_dir()

HISTORY_PATH = _AGENT_DIR / "tui_conversation_history.json"

TIER3_PREFIXES = (
    "pip ",
    "pip3 ",
    "curl ",
    "wget ",
    "npm install",
    "npm i ",
    "cargo install",
    "go install",
    "go get",
    "apt ",
    "apt-get ",
    "yum ",
    "dnf ",
    "brew ",
    "sudo ",
    "su ",
    "chmod ",
    "chown ",
    "rm ",
    "del ",
)

# TUI-02: Map TUI-expected event names → CodingAgent-published event names.
# When the bridge subscribes to a TUI event name, the actual bus subscription
# is made against the CodingAgent name so events are received correctly.
_EVENT_MAP: dict[str, str] = {
    "orchestrator.startup": "system.startup",
    "tool.execute.start": "tool.call.start",
    "tool.execute.finish": "tool.call.finish",
    "tool.execute.error": "tool.call.error",
    "plan.requested": "plan.mode",
    "log.new": "log.line",
    "task.file_modified": "file.modified",
    "delegation.start": "delegation.start",
    "delegation.finish": "delegation.finish",
    "spawn.permission_required": "spawn.permission_required",
}

# AUTO-03: Map TUI role names to CodingAgent system prompt names.
TUI_ROLE_TO_PROMPT: dict[str, str] = {
    "lead_architect": "strategic",  # planning, design
    "full_stack_engineer": "operational",  # execution, coding
    "qa_lead": "reviewer",  # review, testing
}


def _load_copilot_auth_module():
    """Load github_copilot_auth by absolute file path.

    In the TUI context ``src`` in sys.modules is remapped to ``tui/src``,
    so ``from src.core.inference...`` would fail.  Loading by file path
    bypasses the shadow entirely.

    The module is registered in sys.modules under the fake name so that
    Python's @dataclass decorator (which looks up cls.__module__ in
    sys.modules) works correctly.  Subsequent calls return the cached module.
    """
    import importlib.util
    import sys
    from pathlib import Path

    _MOD_NAME = "_copilot_auth_real"
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]

    # core_bridge.py is at tui/src/ui/core_bridge.py → parents[3] = project root
    auth_path = (
        Path(__file__).parents[3]
        / "src"
        / "core"
        / "inference"
        / "adapters"
        / "github_copilot_auth.py"
    )
    spec = importlib.util.spec_from_file_location(_MOD_NAME, str(auth_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec from {auth_path}")
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module so @dataclass can resolve cls.__module__
    sys.modules[_MOD_NAME] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    except Exception:
        sys.modules.pop(_MOD_NAME, None)
        raise
    return mod


def _get_event_bus():
    """Return the real EventBus, falling back to MockEventBus."""
    try:
        from src.core.orchestration.event_bus import get_event_bus  # type: ignore[import]

        return get_event_bus()
    except Exception:
        from tui.tui_src.ui.mock_eventbus import get_mock_event_bus

        return get_mock_event_bus()


def _get_orchestrator(app_obj):
    """Return the real Orchestrator if CodingAgentApp is present."""
    try:
        return getattr(app_obj, "orchestrator", None)
    except Exception:
        return None


def _make_orchestrator(working_dir: Optional[Path], event_bus) -> "Optional[Any]":
    """Create and return a real Orchestrator wired to *event_bus*.

    Falls back to None gracefully if ``src.core`` is unavailable (dev/mock mode).
    """
    try:
        from src.core.orchestration.orchestrator import Orchestrator  # type: ignore[import]

        # HIST-01: Pass message_max_tokens=None so MessageManager acts as a
        # pure accumulator and never silently drops conversation turns.  The
        # default of 4 000 tokens is less than the system prompt alone (~4 500),
        # which caused _truncate_to_window to fire on every append and wipe
        # history on every follow-up message.  The context_builder already
        # handles fitting messages into the LLM's actual context window.
        return Orchestrator(
            working_dir=working_dir,
            event_bus=event_bus,
            message_max_tokens=None,
        )
    except Exception as exc:
        logger.debug(f"_make_orchestrator: could not create Orchestrator: {exc}")
        return None


# ── Mixin method index (for static analysis / test discovery) ────────────────
# BridgeSubscriptionsMixin:  setup_subscriptions, _seed_context_window_from_config, cleanup
# BridgeProviderMixin:       _publish_system_settings, _publish_active_provider_status,
#                            _check_provider_auth_on_startup, _on_orchestrator_startup,
#                            _on_system_settings, _on_provider_status, _on_models_list,
#                            _on_model_routing, _on_model_response, _on_model_token,
#                            _on_stream_chunk, _on_provider_context_window, get_fast_model
# BridgeToolsMixin:          _on_tool_start, _on_tool_finish, _on_tool_error,
#                            _on_delegation_start, _on_delegation_finish, _on_diff_preview,
#                            _on_file_modified, _on_file_deleted, _on_plan_progress,
#                            _on_plan_requested, _on_mcp_server_status,
#                            _on_tool_permission_required (tool.permission_required),
#                            _on_spawn_permission_required (ToolPermissionEvent / SpawnPermissionEvent),
#                            _on_doom_loop_detected, _on_agent_message,
#                            approve_plan, reject_plan, bash_approved, bash_denied,
#                            confirm_file_preview, reject_file_preview
# BridgeContextMixin:        _on_token_budget, _on_token_budget_warning,
#                            _on_context_degraded, _on_context_compacted,
#                            _on_task_queue_updated, _on_step_start, _on_step_finish,
#                            step.start, step.finish, mcp.server.status,
#                            _on_role_transition, _on_preview_pending, _on_git_branch,
#                            _on_retry_attempt, _on_retry_succeeded, _on_retry_failed,
#                            _on_session_new, _on_session_hydrated, _on_session_health,
#                            _on_ui_notification, _on_log_new, _on_usage_turn_summary,
#                            _on_subagent_cost, _get_active_context_length, compact_context
# BridgeSessionMixin:        load_history, _save_history, save_history, clear_history,
#                            undo_last_user_message, _get_prompt_history_path,
#                            load_prompt_history, update_prompt_history,
#                            publish_session_request, publish_session_new,
#                            start_new_session, restore_and_continue
# BridgeAgentMixin:          send_prompt, _run_agent, interrupt, force_interrupt,
#                            pop_pending_injections, get_turn_count, get_usage_totals


class AgentBridge(
    BridgeSubscriptionsMixin,
    BridgeProviderMixin,
    BridgeToolsMixin,
    BridgeContextMixin,
    BridgeSessionMixin,
    BridgeAgentMixin,
):
    """
    Wires the Textual app to the backend EventBus.
    Create one instance per app; call setup_subscriptions() on mount
    and cleanup() on unmount.
    """

    def __init__(self, app: "AgentApp", working_dir: Optional[Path] = None) -> None:
        self.app = app
        self._bus = _get_event_bus()
        self._subscriptions: list[tuple[str, Callable]] = []

        # §9 threading contract
        self._agent_lock = threading.Lock()
        self._agent_running = False
        self._cancel_event = threading.Event()
        self._history_lock = threading.Lock()
        self.history: list[tuple[str, str]] = []
        # MID-INJ: Buffer for messages sent while the agent is running.
        # Consumed by perception_node via pop_pending_injections().
        self._pending_injections: list[str] = []

        # TUI-01: Use real Orchestrator wired to the real EventBus.
        # Try the app's orchestrator attribute first (legacy path), then
        # create one if missing.
        self._orchestrator = _get_orchestrator(app)
        if self._orchestrator is None:
            _wd = working_dir or (Path.cwd())
            self._orchestrator = _make_orchestrator(_wd, self._bus)

        self._working_dir: str = str(working_dir) if working_dir else ""
        self._continue_state: Optional[dict] = None

        # Thread pool for background agent runs (reuse threads, avoid per-call overhead)
        self._thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bridge")

        # AUTO-03: track active TUI role for system-prompt routing
        self._active_role: str = "lead_architect"
        self._deferred_init_done: bool = False

    def _ensure_deferred_init(self) -> None:
        """Run trusted-gated deferred init once per bridge lifetime.

        HIGH-11 fix: ``asyncio.run()`` raises ``RuntimeError`` when called
        from a thread that already has a running event loop (e.g. when
        Textual places coroutine scheduling state on a worker thread).  Use
        an explicit ``new_event_loop`` + ``run_until_complete`` + ``close``
        pattern instead — this is always safe from any thread and does not
        interfere with the main Textual event loop.
        """
        if self._deferred_init_done:
            return
        if self._orchestrator is None:
            self._deferred_init_done = True
            return
        try:
            from src.core.orchestration.deferred_init import (  # type: ignore[import]
                run_deferred_init,
            )

            _loop = asyncio.new_event_loop()
            try:
                _loop.run_until_complete(run_deferred_init(self._orchestrator))
            finally:
                _loop.close()
        except Exception as exc:
            logger.debug(f"deferred_init skipped/failed: {exc}")
        finally:
            self._deferred_init_done = True

    # ── Scheduling UI updates from any thread ─────────────────────────────

    def _schedule_callback(self, fn: Callable, *args) -> None:
        """Thread-safe bridge to Textual's event loop (§9.6)."""
        try:
            loop = asyncio.get_running_loop()
            # Already inside the event loop (mock engine async tasks)
            loop.call_soon(fn, *args)
        except RuntimeError:
            # Background thread — use Textual's thread-safe caller
            try:
                self.app.call_from_thread(fn, *args)
            except Exception as e:
                logger.debug(f"_schedule_callback (call_from_thread) failed: {e}")

    # ── EventBus subscription management (§4.2) ──────────────────────────

    def _subscribe(self, event: str, cb: Callable) -> None:
        # TUI-02: remap TUI event names to the names CodingAgent actually publishes
        actual = _EVENT_MAP.get(event, event)
        self._bus.subscribe(actual, cb)
        self._subscriptions.append((actual, cb))

    def _post(self, msg) -> None:
        self._schedule_callback(self.app.post_message, msg)

    # ── Public read properties ────────────────────────────────────────────

    @property
    def working_dir(self) -> str:
        return self._working_dir

    @working_dir.setter
    def working_dir(self, value: str) -> None:
        self._working_dir = value

    def get_status(self) -> dict:
        """Return a snapshot of bridge/agent state for /status command."""
        orch = self._orchestrator
        task_id: str = "mock"
        if orch:
            get_tid = getattr(orch, "get_current_task_id", None)
            if callable(get_tid):
                try:
                    task_id = str(get_tid())
                except Exception:
                    pass
            else:
                task_id = str(getattr(orch, "current_task_id", "—"))
        return {
            "running": self.is_running(),
            "task_id": task_id,
            "working_dir": self._working_dir,
            "history_len": len(self.history),
        }

    def is_running(self) -> bool:
        with self._agent_lock:
            return self._agent_running

    # ── Bus publish helper ────────────────────────────────────────────────

    def publish(self, event: str, payload: dict | None = None) -> None:
        self._bus.publish(event, payload or {})
