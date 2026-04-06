"""
AgentBridge: Connects the Textual UI to the backend EventBus.

When src.core is available, subscribes to the real EventBus obtained via
get_event_bus().  Otherwise falls back to the MockEventBus so mock_engine.py
keeps working in dev mode.

Implements the full threading contract from §9 of the spec:
  _agent_lock, _cancel_event, _history_lock, send_prompt(), interrupt(),
  force_interrupt(), _run_agent(), _schedule_callback().

History persistence: §15 — atomic JSON at ~/.coding_agent/tui_conversation_history.json
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from src.ui.app import AgentApp

from src.ui.logging import get_logger

logger = get_logger("bridge")

HISTORY_PATH = Path.home() / ".coding_agent" / "tui_conversation_history.json"

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
    "model.routing": "provider.active",
    "log.new": "log.line",
    "task.file_modified": "file.modified",
}

# AUTO-03: Map TUI role names to CodingAgent system prompt names.
TUI_ROLE_TO_PROMPT: dict[str, str] = {
    "lead_architect": "strategic",  # planning, design
    "full_stack_engineer": "operational",  # execution, coding
    "qa_lead": "reviewer",  # review, testing
}


def _get_event_bus():
    """Return the real EventBus, falling back to MockEventBus."""
    try:
        from src.core.orchestration.event_bus import get_event_bus  # type: ignore[import]

        return get_event_bus()
    except Exception:
        from src.ui.mock_eventbus import get_mock_event_bus

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

        return Orchestrator(working_dir=working_dir, event_bus=event_bus)
    except Exception as exc:
        logger.debug(f"_make_orchestrator: could not create Orchestrator: {exc}")
        return None


class AgentBridge:
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

        # TUI-01: Use real Orchestrator wired to the real EventBus.
        # Try the app's orchestrator attribute first (legacy path), then
        # create one if missing.
        self._orchestrator = _get_orchestrator(app)
        if self._orchestrator is None:
            _wd = working_dir or (Path.cwd())
            self._orchestrator = _make_orchestrator(_wd, self._bus)

        self._working_dir: str = str(working_dir) if working_dir else ""
        self._continue_state: Optional[dict] = None

        # AUTO-03: track active TUI role for system-prompt routing
        self._active_role: str = "lead_architect"

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

    def setup_subscriptions(self) -> None:
        """Subscribe to every event in §4.5."""
        # provider / model
        self._subscribe("orchestrator.startup", self._on_orchestrator_startup)
        self._subscribe("provider.status.changed", self._on_provider_status)
        self._subscribe("provider.models.list", self._on_models_list)
        self._subscribe("provider.models.cached", self._on_models_list)
        self._subscribe("provider.models.empty", lambda p: None)
        self._subscribe("provider.model.missing", lambda p: None)
        self._subscribe("model.routing", self._on_model_routing)
        self._subscribe("model.response", self._on_model_response)
        self._subscribe("model.token", self._on_model_token)
        # TUI-10: per-chunk reasoning routing
        self._subscribe("response.stream_chunk", self._on_stream_chunk)
        # tool
        self._subscribe("tool.execute.start", self._on_tool_start)
        self._subscribe("tool.invoked", lambda p: None)
        self._subscribe("tool.execute.finish", self._on_tool_finish)
        self._subscribe("tool.execute.error", self._on_tool_error)
        # file
        self._subscribe("file.diff.preview", self._on_diff_preview)
        self._subscribe("file.modified", self._on_file_modified)
        self._subscribe("file.deleted", self._on_file_deleted)
        # plan
        self._subscribe("plan.progress", self._on_plan_progress)
        self._subscribe("plan.requested", self._on_plan_requested)
        # session
        self._subscribe("session.new", self._on_session_new)
        self._subscribe("session.hydrated", self._on_session_hydrated)
        self._subscribe("session.registered", lambda p: None)
        self._subscribe("session.unregistered", lambda p: None)
        self._subscribe("session.health_alert", self._on_session_health)
        # notifications / logging
        self._subscribe("ui.notification", self._on_ui_notification)
        self._subscribe("log.new", self._on_log_new)
        # token budget
        self._subscribe("token.budget.update", self._on_token_budget)
        self._subscribe("token.budget.warning", self._on_token_budget_warning)
        # role transition — real backend and mock both publish via bus
        self._subscribe("role.transition", self._on_role_transition)
        # preview mode
        self._subscribe("preview.pending", self._on_preview_pending)
        self._subscribe("preview.confirmed", lambda p: None)
        self._subscribe("preview.rejected", lambda p: None)
        # git status
        self._subscribe("git.branch", self._on_git_branch)
        # retry / resilience
        self._subscribe("retry.attempt", self._on_retry_attempt)
        self._subscribe("retry.succeeded", self._on_retry_succeeded)
        self._subscribe("retry.failed", self._on_retry_failed)
        # context
        self._subscribe("context.degraded", self._on_context_degraded)
        self._subscribe("context.compacted", self._on_context_compacted)
        # task queue
        self._subscribe("task.queue.updated", self._on_task_queue_updated)
        # step boundaries (opencode-style)
        self._subscribe("step.start", self._on_step_start)
        self._subscribe("step.finish", self._on_step_finish)
        # MCP server status
        self._subscribe("mcp.server.status", self._on_mcp_server_status)
        # tool permission gate
        self._subscribe("tool.permission_required", self._on_tool_permission_required)
        # per-turn token/cost summary (TUI-T6)
        self._subscribe("usage.turn_summary", self._on_usage_turn_summary)
        # doom-loop detection (PERM-W3)
        self._subscribe("tool.doom_loop_detected", self._on_doom_loop_detected)

        logger.info(f"EventBus: subscribed to {len(self._subscriptions)} events")

    def cleanup(self) -> None:
        """Unsubscribe all handlers (§10.2 step 5)."""
        for event, cb in self._subscriptions:
            try:
                self._bus.unsubscribe(event, cb)
            except Exception:
                pass
        self._subscriptions.clear()
        logger.info("EventBus: all subscriptions removed")

    # ── EventBus → Textual message translators ────────────────────────────

    def _publish_system_settings(self) -> None:
        """TUI-11: Respond to RequestSystemSettings() from AgentApp.on_mount().

        Publishes a ``system.settings`` event that AgentApp converts to a
        ``SystemSettingsLoaded`` Textual message.  Falls back gracefully when
        the orchestrator or config are unavailable (dev / mock mode).
        """
        try:
            from src.core.config_loader import load_merged_config

            cfg = load_merged_config(
                Path(self._working_dir) if self._working_dir else None
            )
        except Exception:
            cfg = {}

        # Gather available providers from config (best-effort)
        providers: list[dict] = []
        try:
            providers = [
                {"name": p.get("name", ""), "type": p.get("type", "")}
                for p in cfg.get("providers", [])
                if isinstance(p, dict)
            ]
        except Exception:
            pass

        try:
            self._bus.publish(
                "system.settings",
                {
                    "active_mode": cfg.get("active_mode", "lead_architect"),
                    "theme": cfg.get("theme", "textual-dark"),
                    "context_window": cfg.get("max_tokens", 32_768),
                    "default_provider": cfg.get("default_provider", "none"),
                    "default_model": cfg.get("default_model", "none"),
                    "providers": providers,
                    "autonomous_mode": cfg.get("autonomous_mode", False),
                    "max_turns": cfg.get("max_turns", 50),
                },
            )
        except Exception as exc:
            logger.debug(f"_publish_system_settings: {exc}")

        # Also publish startup / running events so UI banners reflect state
        try:
            self._bus.publish(
                "orchestrator.startup",
                {"working_dir": self._working_dir or str(Path.cwd())},
            )
        except Exception:
            pass
        try:
            from src.ui.bus import AgentRunningEvent

            self._post(AgentRunningEvent(running=False))
        except Exception:
            pass

    def _post(self, msg) -> None:
        self._schedule_callback(self.app.post_message, msg)

    def _on_orchestrator_startup(self, payload: dict) -> None:
        from src.ui.bus import OrchestratorReadyEvent

        wd = payload.get("working_dir", "")
        self._working_dir = wd
        self._post(OrchestratorReadyEvent(working_dir=wd))

    def _on_provider_status(self, payload: dict) -> None:
        from src.ui.bus import ProviderStatusChangeEvent

        self._post(
            ProviderStatusChangeEvent(
                provider=payload.get("provider", ""),
                new_status=payload.get("status", ""),
                old_status="",
            )
        )

    def _on_models_list(self, payload: dict) -> None:
        logger.debug(
            f"Models: {payload.get('provider')} — {len(payload.get('models', []))} models"
        )

    def _on_model_routing(self, payload: dict) -> None:
        from src.ui.bus import ModelRoutingEvent

        self._post(
            ModelRoutingEvent(
                provider=payload.get("provider", ""),
                model=payload.get("selected", ""),
            )
        )

    def _on_model_response(self, payload: dict) -> None:
        tokens = payload.get("tokens", 0)
        logger.info(f"Model response complete: {tokens} tokens")

    def _on_model_token(self, payload: dict) -> None:
        from src.ui.bus import StreamChunkEvent

        text = payload.get("text", "")
        partial = payload.get("partial", True)
        self._post(StreamChunkEvent(chunk=text, is_partial=partial))

    def _on_stream_chunk(self, payload: dict) -> None:
        """TUI-10: Route stream chunks to StreamChunkEvent or DisplayReasoning."""
        is_reasoning = payload.get("is_reasoning", False)
        chunk = payload.get("chunk", "")
        if not chunk:
            return
        if is_reasoning:
            try:
                import time as _time
                from src.ui.bus import DisplayReasoning

                self._post(DisplayReasoning(content=chunk, start_time=_time.time()))
            except ImportError:
                # DisplayReasoning may not exist in all TUI versions; fall back silently
                pass
        else:
            from src.ui.bus import StreamChunkEvent

            self._post(StreamChunkEvent(chunk=chunk, is_partial=True))

    def _on_tool_start(self, payload: dict) -> None:
        from src.ui.bus import ToolCallStartEvent, BashApprovalEvent

        tool_name = payload.get("title") or payload.get("tool", "unknown")
        tool_args = payload.get("rawInput") or payload.get("args", {})
        tool_id = payload.get("toolCallId", "")
        if not isinstance(tool_args, dict):
            tool_args = {}

        # §16.1 — bash tier-3 gate
        if tool_name == "bash":
            cmd = tool_args.get("command", "").lower().strip()
            if any(cmd.startswith(p) for p in TIER3_PREFIXES):
                self._post(
                    BashApprovalEvent(
                        tool_id=tool_id, command=tool_args.get("command", "")
                    )
                )
                return

        self._post(
            ToolCallStartEvent(
                tool_name=tool_name, tool_args=tool_args, tool_id=tool_id
            )
        )

    def _on_tool_finish(self, payload: dict) -> None:
        from src.ui.bus import ToolCallFinishEvent

        tool_name = payload.get("title") or payload.get("tool", "unknown")
        tool_id = payload.get("toolCallId", "")
        content = payload.get("content", [])
        if content and isinstance(content, list) and isinstance(content[0], dict):
            result_text = content[0].get("text", "")
        else:
            result_text = str(
                payload.get("result_formatted") or payload.get("result", "")
            )
        ok = payload.get("ok", True)
        self._post(
            ToolCallFinishEvent(
                tool_name=tool_name, tool_id=tool_id, result_text=result_text, ok=ok
            )
        )

    def _on_tool_error(self, payload: dict) -> None:
        from src.ui.bus import ToolCallErrorEvent

        tool_name = payload.get("title") or payload.get("tool", "unknown")
        tool_id = payload.get("toolCallId", "")
        self._post(
            ToolCallErrorEvent(
                tool_name=tool_name,
                tool_id=tool_id,
                error=str(payload.get("error", "Unknown error")),
            )
        )

    def _on_diff_preview(self, payload: dict) -> None:
        from src.ui.bus import DiffPreviewEvent

        self._post(
            DiffPreviewEvent(
                path=payload.get("path", ""),
                diff=payload.get("diff", ""),
                is_new_file=payload.get("is_new_file", False),
            )
        )

    def _on_file_modified(self, payload: dict) -> None:
        from src.ui.bus import FileModifiedEvent

        self._post(FileModifiedEvent(file_path=payload.get("path", ""), diff=""))

    def _on_file_deleted(self, payload: dict) -> None:
        from src.ui.bus import FileModifiedEvent

        self._post(
            FileModifiedEvent(file_path=f"[deleted] {payload.get('path', '')}", diff="")
        )

    def _on_plan_progress(self, payload: dict) -> None:
        from src.ui.bus import PlanProgressEvent

        # Accept both ACP and legacy schemas (§12.3).
        # Use explicit int() coercion with a guaranteed int fallback so pyright
        # knows step/total are always int (payload values are Any/Unknown).
        _raw_step = (
            payload.get("currentStep")
            if payload.get("currentStep") is not None
            else payload.get("step")
        )
        _raw_total = (
            payload.get("totalSteps")
            if payload.get("totalSteps") is not None
            else payload.get("total")
        )
        step: int = int(_raw_step) if _raw_step is not None else 0
        total: int = int(_raw_total) if _raw_total is not None else 0
        desc = payload.get("stepDescription") or payload.get("description", "")
        self._post(PlanProgressEvent(step=step, total=total, description=desc))

    def _on_plan_requested(self, payload: dict) -> None:
        from src.ui.bus import PlanRequestedEvent

        self._post(PlanRequestedEvent(plan_text=payload.get("plan_text", "")))

    def _on_session_new(self, payload: dict) -> None:
        self._schedule_callback(self.app._handle_session_new)

    def _on_session_hydrated(self, payload: dict) -> None:
        logger.info("Session hydrated from backend")

    def _on_session_health(self, payload: dict) -> None:
        from src.ui.bus import SessionHealthEvent

        self._post(
            SessionHealthEvent(
                level=payload.get("level", "info"),
                title=payload.get("title", ""),
                message=payload.get("message", ""),
            )
        )

    def _on_ui_notification(self, payload: dict) -> None:
        from src.ui.bus import NotificationEvent

        self._post(
            NotificationEvent(
                level=payload.get("level", "info"),
                message=payload.get("message", ""),
                source=payload.get("source", ""),
            )
        )

    def _on_log_new(self, payload: dict) -> None:
        """§16.4 — write DIRECTLY to console panel; never through Python logging."""
        level = payload.get("level", "INFO").upper()
        logger_name = payload.get("logger", "")
        msg = payload.get("message", "")
        line = f"[{level}] {logger_name}: {msg}" if logger_name else f"[{level}] {msg}"
        self._schedule_callback(self.app._append_log_line, line, level)

    def _on_token_budget(self, payload: dict) -> None:
        from src.ui.bus import TokenBudgetEvent

        self._post(
            TokenBudgetEvent(
                used=payload.get("used", 0),
                limit=payload.get("limit", 32000),
                percent=payload.get("percent", 0.0),
                warning=False,
            )
        )

    def _on_token_budget_warning(self, payload: dict) -> None:
        from src.ui.bus import TokenBudgetEvent

        self._post(
            TokenBudgetEvent(
                used=payload.get("used", 0),
                limit=payload.get("limit", 32000),
                percent=payload.get("percent", 0.0),
                warning=True,
            )
        )

    def _on_role_transition(self, payload: dict) -> None:
        """Real backend fires role.transition via EventBus (mock uses direct post_message)."""
        from src.ui.bus import RoleTransitionEvent

        self._post(
            RoleTransitionEvent(
                from_role=payload.get("from_role", "system"),
                to_role=payload.get("to_role", "lead_architect"),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_preview_pending(self, payload: dict) -> None:
        from src.ui.bus import DiffPreviewEvent

        self._post(
            DiffPreviewEvent(
                path=payload.get("path") or payload.get("tool", ""),
                diff=payload.get("diff", ""),
                is_new_file=payload.get("is_new_file", False),
            )
        )

    def _on_git_branch(self, payload: dict) -> None:
        from src.ui.bus import GitBranchEvent

        self._post(
            GitBranchEvent(
                branch=payload.get("branch", "main"),
                dirty=payload.get("dirty", False),
                ahead=payload.get("ahead", 0),
                behind=payload.get("behind", 0),
            )
        )

    def _on_retry_attempt(self, payload: dict) -> None:
        from src.ui.bus import RetryAttemptEvent

        self._post(
            RetryAttemptEvent(
                attempt_number=payload.get("attempt_number", 0),
                max_attempts=payload.get("max_attempts", 0),
                error_type=payload.get("error_type", ""),
                provider=payload.get("provider", ""),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_retry_succeeded(self, payload: dict) -> None:
        from src.ui.bus import RetrySucceededEvent

        self._post(
            RetrySucceededEvent(
                attempt_number=payload.get("attempt_number", 0),
                provider=payload.get("provider", ""),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_retry_failed(self, payload: dict) -> None:
        from src.ui.bus import RetryFailedEvent

        self._post(
            RetryFailedEvent(
                total_attempts=payload.get("total_attempts", 0),
                error_type=payload.get("error_type", ""),
                provider=payload.get("provider", ""),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_context_degraded(self, payload: dict) -> None:
        from src.ui.bus import ContextDegradedEvent

        self._post(
            ContextDegradedEvent(
                target_window=payload.get("target_window", 0),
                reason=payload.get("reason", ""),
            )
        )

    def _on_context_compacted(self, payload: dict) -> None:
        """S9-B: Notify UI when /compact finishes context distillation."""
        from src.ui.bus import NotificationEvent

        self._post(
            NotificationEvent(
                level="information",
                message=payload.get("message", "Context compacted"),
                source="compact",
            )
        )

    def _on_task_queue_updated(self, payload: dict) -> None:
        from src.ui.bus import TaskQueueUpdatedEvent

        self._post(
            TaskQueueUpdatedEvent(
                queue_size=payload.get("queue_size", 0),
                pending_count=payload.get("pending_count", 0),
                task_id=payload.get("task_id"),
                old_status=payload.get("old_status"),
                new_status=payload.get("new_status"),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_step_start(self, payload: dict) -> None:
        from src.ui.bus import StepStartEvent

        self._post(
            StepStartEvent(
                tool=payload.get("tool", ""),
                step=int(payload.get("step", 0) or 0),
                total=int(payload.get("total", 0) or 0),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_step_finish(self, payload: dict) -> None:
        from src.ui.bus import StepFinishEvent

        elapsed = payload.get("elapsed_ms")
        self._post(
            StepFinishEvent(
                tool=payload.get("tool", "?"),
                ok=payload.get("ok", True),
                elapsed_ms=int(elapsed) if elapsed is not None else None,
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_mcp_server_status(self, payload: dict) -> None:
        from src.ui.bus import McpServerStatusEvent

        self._post(
            McpServerStatusEvent(
                running=payload.get("running", False),
                count=payload.get("count", 0),
                server_names=payload.get("server_names", []),
            )
        )

    def _on_tool_permission_required(self, payload: dict) -> None:
        from src.ui.bus import ToolPermissionEvent

        self._post(
            ToolPermissionEvent(
                tool=payload.get("tool", "unknown"),
                args=payload.get("args", {}),
                tool_id=payload.get("tool_id", ""),
            )
        )

    def _on_usage_turn_summary(self, payload: dict) -> None:
        """TUI-T6: forward per-turn token/cost summary to TUI."""
        from src.ui.bus import UsageTurnSummaryEvent

        self._post(
            UsageTurnSummaryEvent(
                input_tokens=int(payload.get("input_tokens", 0)),
                output_tokens=int(payload.get("output_tokens", 0)),
                model=str(payload.get("model", "")),
                cost_usd=float(payload.get("cost_usd", 0.0)),
            )
        )

    def _on_doom_loop_detected(self, payload: dict) -> None:
        """PERM-W3: forward doom-loop detection to TUI for user confirmation."""
        from src.ui.bus import DoomLoopEvent

        self._post(
            DoomLoopEvent(
                tool_name=str(payload.get("tool_name", "")),
                fingerprint=str(payload.get("fingerprint", "")),
                count=int(payload.get("count", 3)),
                tool_id=str(payload.get("tool_id", "")),
            )
        )

    # ── Agent running / threading contract (§9) ───────────────────────────

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

    def get_fast_model(self) -> dict:
        """S8-C: Return the NANO-tier model from config (for /fast command).

        Checks ``model_routing.nano_model`` in the loaded config.  Returns a
        dict with a ``model`` key when found; empty dict otherwise.
        """
        try:
            from src.core.config_loader import load_merged_config

            cfg = load_merged_config()
            nano_model = (cfg.get("model_routing") or {}).get("nano_model")
            if nano_model:
                return {"model": nano_model, "tier": "nano"}
        except Exception:
            pass
        return {}

    def compact_context(self) -> bool:
        """Attempt to compact context on the orchestrator. Returns True if successful."""
        orch = self._orchestrator
        if not orch:
            return False
        for method in ("compact_context", "compact", "flush_execution_trace"):
            fn = getattr(orch, method, None)
            if callable(fn):
                try:
                    fn()
                    logger.info(f"compact_context: called orchestrator.{method}()")
                    return True
                except Exception as exc:
                    logger.warning(f"compact_context: {method}() failed: {exc}")
        return False

    def is_running(self) -> bool:
        with self._agent_lock:
            return self._agent_running

    def send_prompt(self, text: str) -> bool:
        """Thread-safe prompt submission. Returns False if already running."""
        with self._agent_lock:
            if self._agent_running:
                return False
            self._agent_running = True
        self._cancel_event.clear()
        with self._history_lock:
            self.history.append(("user", text))
        threading.Thread(target=self._run_agent, args=(text,), daemon=True).start()
        return True

    def interrupt(self) -> None:
        """Single Escape — set cancel event (§9.4)."""
        self._cancel_event.set()

    def force_interrupt(self) -> None:
        """Double-Escape — force stop (§9.5)."""
        self._cancel_event.set()
        with self._agent_lock:
            running = self._agent_running
            if running:
                self._agent_running = False
        if running:
            from src.ui.bus import AgentRunningEvent

            self._post(AgentRunningEvent(running=False))

    def _run_agent(self, text: str) -> None:
        """Run the agent on a background thread (§7.1)."""
        from src.ui.bus import AgentFinalResponse, WorkerError, AgentRunningEvent

        try:
            self._post(AgentRunningEvent(running=True))
            if self._orchestrator:
                self._orchestrator.start_new_task()
                # AUTO-02: apply per-role autonomy settings before each run
                try:
                    from src.core.config_loader import get_role_config
                    from src.tools.tools_config import set_autonomous

                    _wdir = Path(self._working_dir) if self._working_dir else None
                    role_cfg = get_role_config(self._active_role, working_dir=_wdir)
                    set_autonomous(bool(role_cfg.get("autonomous", False)))
                    # Apply max_turns to the graph state (best-effort)
                    _max_turns = role_cfg.get("max_turns", 50)
                except Exception as _rc_err:
                    logger.debug(f"AUTO-02: role config apply failed: {_rc_err}")
                    _max_turns = 50

                # AUTO-03: map TUI role name to CodingAgent system-prompt name
                prompt_name = TUI_ROLE_TO_PROMPT.get(self._active_role, "operational")

                # Use public message-list API; fall back gracefully if shape differs
                msg_mgr = getattr(self._orchestrator, "msg_mgr", None) or getattr(
                    self._orchestrator, "message_manager", None
                )
                if msg_mgr is not None:
                    messages = list(getattr(msg_mgr, "messages", []))
                elif callable(getattr(self._orchestrator, "get_messages", None)):
                    messages = list(self._orchestrator.get_messages())
                else:
                    messages = []
                tools = self._orchestrator.get_tools_for_role("operational")
                # HIGH-11 fix: use a dedicated event loop instead of asyncio.run().
                # asyncio.run() creates a new loop but also installs it as the
                # running loop for the current thread for its duration, which can
                # conflict if this thread already has a loop set (e.g. from a prior
                # call that was not fully torn down).  Creating an explicit loop and
                # running it to completion is safe on any background thread.
                _loop = asyncio.new_event_loop()
                try:
                    result = _loop.run_until_complete(
                        self._orchestrator.run_agent_once(
                            system_prompt_name=prompt_name,
                            messages=messages,
                            tools=tools,
                            cancel_event=self._cancel_event,
                        )
                    )
                finally:
                    _loop.close()
                self._orchestrator.flush_execution_trace()
                content = result.get("response") or result.get("last_result", {}).get(
                    "output", ""
                )
                if content:
                    with self._history_lock:
                        self.history.append(("assistant", content))
                    self._save_history()
                    self._post(AgentFinalResponse(content=content))
                # §10.4 — capture state for /continue
                self.app._continue_state = result
            # else: mock mode — events come through EventBus from mock_engine
        except Exception as exc:
            logger.error(f"Agent error: {exc}", exc_info=True)
            self._post(WorkerError(message=str(exc), traceback=""))
        finally:
            with self._agent_lock:
                self._agent_running = False
            self._post(AgentRunningEvent(running=False))
            # TASK-05: persist session snapshot after each agent run so headless /
            # autonomous runs are captured even without a UI quit event.
            try:
                self.app._save_session_snapshot()
            except Exception as _snap_err:
                logger.debug(f"_run_agent: session snapshot failed: {_snap_err}")

    # ── Plan approval ─────────────────────────────────────────────────────

    def approve_plan(self) -> None:
        if self._orchestrator:
            self._orchestrator.approve_plan()
        self._bus.publish("preview.confirmed", {"preview_id": "plan"})

    def reject_plan(self) -> None:
        if self._orchestrator:
            self._orchestrator.reject_plan()
        self._bus.publish("preview.rejected", {"preview_id": "plan"})

    def bash_approved(self, tool_id: str) -> None:
        # TUI-03: publish bash.approval_granted so the backend gate resolves
        self._bus.publish("bash.approval_granted", {"tool_id": tool_id})
        # Legacy preview event for backwards-compat
        self._bus.publish("preview.confirmed", {"preview_id": tool_id})

    def bash_denied(self, tool_id: str) -> None:
        # TUI-03: publish bash.approval_denied so the backend gate releases
        self._bus.publish("bash.approval_denied", {"tool_id": tool_id})
        # Legacy preview event for backwards-compat
        self._bus.publish("preview.rejected", {"preview_id": tool_id})

    # ── TASK-05: orchestrator accessors for session persistence ──────────

    def get_turn_count(self) -> int:
        """Return the current turn counter from the orchestrator's graph state.

        Falls back to 0 if the orchestrator is unavailable or the state key
        is not present (mock mode).
        """
        try:
            if self._orchestrator is None:
                return 0
            # The orchestrator exposes the latest state via _last_state or
            # the graph's current state; try several attribute names.
            for attr in ("_last_state", "_current_state", "_agent_state"):
                state = getattr(self._orchestrator, attr, None)
                if isinstance(state, dict) and "turn_count" in state:
                    return int(state["turn_count"] or 0)
            # Fallback: count message pairs in our local history
            with self._history_lock:
                assistant_msgs = sum(
                    1 for role, _ in self.history if role == "assistant"
                )
            return assistant_msgs
        except Exception:
            return 0

    def get_usage_totals(self) -> tuple[int, int]:
        """Return ``(input_tokens, output_tokens)`` accumulated this session.

        Reads from the orchestrator's token-budget monitor when available;
        falls back to (0, 0) in mock mode.
        """
        try:
            if self._orchestrator is None:
                return (0, 0)
            monitor = getattr(self._orchestrator, "token_monitor", None)
            if monitor is None:
                return (0, 0)
            task_id = (
                getattr(self._orchestrator, "_current_task_id", "default") or "default"
            )
            budget = monitor.get_budget(session_id=task_id)
            return (int(budget.prompt_tokens), int(budget.completion_tokens))
        except Exception:
            return (0, 0)

    # ── Session events ────────────────────────────────────────────────────

    def publish_session_request(self) -> None:
        """Publish session.request_state on startup (§10.1 step 6)."""
        self._bus.publish("session.request_state", {"session_id": "default"})

    def publish_session_new(self) -> None:
        """Publish session.new on /new command (§10.3)."""
        self._bus.publish("session.new", {"timestamp": time.time()})

    def start_new_session(self) -> None:
        """Public: reset orchestrator task state + publish session.new (§10.3)."""
        if self._orchestrator:
            start_fn = getattr(self._orchestrator, "start_new_task", None)
            if callable(start_fn):
                try:
                    start_fn()
                except Exception as exc:
                    logger.warning(f"start_new_task() failed: {exc}")
        self.publish_session_new()

    def restore_and_continue(
        self, last_task: str, continue_state: Optional[dict]
    ) -> bool:
        """Restore previous state (if any) and re-submit the last task. Returns False if already running."""
        orch = self._orchestrator
        if orch and continue_state:
            restore_fn = getattr(orch, "restore_continue_state", None)
            if callable(restore_fn):
                try:
                    restore_fn(continue_state)
                    logger.info("restore_and_continue: state restored")
                except Exception as exc:
                    logger.warning(f"restore_and_continue: restore failed: {exc}")
        return self.send_prompt(last_task)

    # ── History persistence (§15) ─────────────────────────────────────────

    # SES-W1: versioned history envelope.  Version 1 wraps the list in a dict
    # so future format changes can be detected and migrated at load time.
    _HISTORY_VERSION = 1

    def load_history(self) -> None:
        """Atomic load on startup (§15.3).

        Supports both the legacy bare-list format (v0) and the current versioned
        envelope format (v1: {"version": 1, "history": [...]}).
        """
        if not HISTORY_PATH.exists():
            return
        try:
            raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            # v1 versioned envelope
            if isinstance(raw, dict) and "history" in raw:
                entries = raw["history"]
            # v0 legacy bare list — migrate transparently
            elif isinstance(raw, list):
                entries = raw
            else:
                entries = []
            with self._history_lock:
                self.history = [
                    tuple(item)
                    for item in entries
                    if isinstance(item, (list, tuple)) and len(item) == 2
                ]
            logger.info(f"History loaded: {len(self.history)} entries")
        except Exception as e:
            logger.warning(f"History load failed (starting fresh): {e}")

    def _save_history(self) -> None:
        """Atomic write after every agent result (§15.4)."""
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(HISTORY_PATH.parent), suffix=".tmp")
        try:
            # LOW-10 fix: if os.fdopen() raises before handing ownership of fd
            # to the file object, we must close fd ourselves to avoid a leak.
            # Wrapping the fdopen() call in a nested try/except achieves this
            # without restructuring the rest of the function.
            try:
                fobj = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                os.close(fd)
                raise
            with fobj:
                with self._history_lock:
                    payload = {
                        "version": self._HISTORY_VERSION,
                        "history": list(self.history),
                    }
                    json.dump(payload, fobj, ensure_ascii=False, indent=2)
            os.replace(tmp, str(HISTORY_PATH))
        except Exception as e:
            logger.error(f"History save failed: {e}")
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def save_history(self) -> None:
        """Public save — call at shutdown."""
        self._save_history()

    def clear_history(self) -> None:
        with self._history_lock:
            self.history.clear()
        self._save_history()

    # ── Frecency-scored prompt history (§H6) ─────────────────────────────

    @staticmethod
    def _get_prompt_history_path() -> Path:
        hist_dir = Path.home() / ".coding_agent"
        hist_dir.mkdir(parents=True, exist_ok=True)
        return hist_dir / "tui_prompt_history.json"

    def load_prompt_history(self) -> list[str]:
        """Load frecency-scored prompt history.

        Returns list of prompt strings sorted by score (most frequent/recent first).
        Score formula: count / ((1 + hours_ago) ** 0.5). Top 500 entries returned.
        """
        try:
            p = self._get_prompt_history_path()
            if not p.exists():
                return []
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            now = time.time()
            entries: list[tuple[float, str]] = []
            for entry in data:
                if not isinstance(entry, dict) or "text" not in entry:
                    continue
                text: str = str(entry["text"])
                count: int = int(entry.get("count", 1))
                last_used: float = float(entry.get("last_used", now))
                hours_ago: float = max(0.0, (now - last_used) / 3600)
                score: float = count / ((1 + hours_ago) ** 0.5)
                entries.append((score, text))
            entries.sort(reverse=True)
            return [t for _, t in entries[:500]]
        except Exception:
            return []

    def update_prompt_history(self, text: str) -> None:
        """Record a prompt submission, update frecency scores, persist atomically."""
        try:
            p = self._get_prompt_history_path()
            now = time.time()
            data: list[dict] = []
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        data = raw
                except Exception:
                    data = []

            # Update existing entry or insert new one
            found = False
            for entry in data:
                if isinstance(entry, dict) and entry.get("text") == text:
                    entry["count"] = int(entry.get("count", 0)) + 1
                    entry["last_used"] = now
                    found = True
                    break
            if not found:
                data.append({"text": text, "count": 1, "last_used": now})

            # Prune to top 500 by frecency score
            def _score(e: dict) -> float:
                hours_ago = max(0.0, (now - float(e.get("last_used", now))) / 3600)
                return int(e.get("count", 1)) / ((1 + hours_ago) ** 0.5)

            data.sort(key=_score, reverse=True)
            data = data[:500]

            # Atomic write via temp file
            fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp_path, str(p))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:
            pass

    # ── Bus publish helper ────────────────────────────────────────────────

    def publish(self, event: str, payload: dict | None = None) -> None:
        self._bus.publish(event, payload or {})
