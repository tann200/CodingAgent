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
    "log.new": "log.line",
    "task.file_modified": "file.modified",
    "delegation.start": "delegation.start",
    "delegation.finish": "delegation.finish",
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
        # canonical token.budget event published by orchestrator TUI-09
        self._subscribe("token.budget", self._on_token_budget)
        # context window from provider (e.g. LM Studio loaded_context_length)
        self._subscribe("provider.context_window", self._on_provider_context_window)
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
        # GAP-NEW-7: subagent cost rollup — accumulate child cost into session total
        self._subscribe("usage.subagent_cost", self._on_subagent_cost)
        # doom-loop detection (PERM-W3)
        self._subscribe("tool.doom_loop_detected", self._on_doom_loop_detected)
        # CP-15: proactive mid-turn messages from send_user_message tool
        self._subscribe("agent.message", self._on_agent_message)
        # SUBAGENT-VIS-3: subagent lifecycle visibility
        self._subscribe("delegation.start", self._on_delegation_start)
        self._subscribe("delegation.finish", self._on_delegation_finish)

        logger.info(f"EventBus: subscribed to {len(self._subscriptions)} events")

        # RACE-FIX: Immediately seed the token monitor and sidebar with the
        # context length from providers.json so the TOKEN BUDGET max is correct
        # on first display, without waiting for the async provider.context_window
        # event that may fire before subscriptions are registered.
        self._seed_context_window_from_config()

    def _seed_context_window_from_config(self) -> None:
        """Seed the token budget monitor and sidebar with the providers.json
        context_length immediately after setup_subscriptions() completes.

        This avoids the race condition where ProviderManager.initialize() fires
        ``provider.context_window`` before setup_subscriptions() has registered
        the ``_on_provider_context_window`` handler, causing the event to be lost
        and the sidebar to fall back to the 32,768 default.
        """
        try:
            from src.core.inference.provider_context import (  # type: ignore[import]
                _load_active_context_length,
                set_active_context_length,
            )

            ctx = _load_active_context_length()
            if ctx and ctx > 0:
                # Persist to the provider_context module so all consumers see it
                set_active_context_length(ctx)

                # Seed the token monitor under the "default" session (used before
                # the first task starts) so get_budget("default").max_tokens is
                # correct when the first token.budget event arrives.
                try:
                    from src.core.orchestration.token_budget import (  # type: ignore[import]
                        get_token_budget_monitor,
                    )

                    get_token_budget_monitor().update(
                        session_id="default", used_tokens=0, max_tokens=ctx
                    )
                except Exception:
                    pass

                # Fire a synthetic UpdateSettings so the sidebar label updates
                # immediately without waiting for a token.budget event.
                try:
                    from src.ui.events import UpdateSettings

                    self._post(UpdateSettings(updates={"context_window": ctx}))
                except Exception:
                    pass
        except Exception:
            pass

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

        # Gather available providers from providers.json (top-level list)
        providers: list[dict] = []
        try:
            import json

            # __file__ = tui/src/ui/core_bridge.py → parents[3] = project root
            _cfg_path = Path(__file__).parents[3] / "src" / "config" / "providers.json"
            if not _cfg_path.exists():
                _cfg_path = Path("src/config/providers.json")
            if _cfg_path.exists():
                raw = json.loads(_cfg_path.read_text(encoding="utf-8"))
                entries = raw if isinstance(raw, list) else (raw.get("providers") or [])
                providers = [
                    {
                        "name": p.get("name") or p.get("type") or "",
                        "type": p.get("type") or "",
                        "models": p.get("models") or [],
                        "active": p.get("active", False),
                        "base_url": p.get("base_url") or "",
                    }
                    for p in entries
                    if isinstance(p, dict)
                ]
        except Exception:
            pass

        # Derive context_window from the active provider's context_length field
        # (providers.json), falling back to cfg.get("max_tokens") or 32_768.
        # This ensures the sidebar TOKEN BUDGET max reflects the real config value
        # rather than a hardcoded default.
        _context_window: int = cfg.get("max_tokens", 0) or 0
        if not _context_window:
            try:
                from src.core.inference.provider_context import (  # type: ignore[import]
                    _load_active_context_length,
                )

                _context_window = _load_active_context_length()
            except Exception:
                _context_window = 32_768
        if not _context_window:
            _context_window = 32_768

        try:
            self._bus.publish(
                "system.settings",
                {
                    "active_mode": cfg.get("active_mode", "lead_architect"),
                    "theme": cfg.get("theme", "textual-dark"),
                    "context_window": _context_window,
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
        # Call directly (don't rely on the bus event being received — the
        # _EVENT_MAP remaps "orchestrator.startup" → "system.startup" for
        # inbound subscriptions, but the above publish uses the TUI name which
        # has no subscriber after setup_subscriptions() maps it.)
        self._publish_active_provider_status()
        self._check_provider_auth_on_startup()
        try:
            from src.ui.bus import AgentRunningEvent

            self._post(AgentRunningEvent(running=False))
        except Exception:
            pass

    def _post(self, msg) -> None:
        self._schedule_callback(self.app.post_message, msg)

    def _on_orchestrator_startup(self, payload: dict) -> None:
        from src.ui.bus import OrchestratorReadyEvent, SessionHealthEvent

        wd = payload.get("working_dir", "")
        self._working_dir = wd
        self._post(OrchestratorReadyEvent(working_dir=wd))

        # Immediately publish the active provider so the banner shows the real name
        # before the slow async ProviderManager.initialize() completes.
        self._publish_active_provider_status()

        # Warn the user if the active provider is GitHub Copilot but has no token.
        self._check_provider_auth_on_startup()

    def _publish_active_provider_status(self) -> None:
        """Read providers.json and immediately fire a provider status event for
        the active provider so the banner shows the real name on startup.

        For GitHub Copilot (which uses OAuth device flow, not a network probe),
        we call validate_connection() / is_authenticated() synchronously so the
        banner immediately reflects real auth state instead of staying at
        "initializing…" until ProviderManager.initialize() completes.
        """
        try:
            from src.ui.bus import ProviderStatusChangeEvent
            import json, pathlib

            providers_path = (
                pathlib.Path(__file__).parents[3] / "src" / "config" / "providers.json"
            )
            if not providers_path.exists():
                # Try relative fallback
                providers_path = pathlib.Path("src/config/providers.json")
            if not providers_path.exists():
                return
            raw = json.loads(providers_path.read_text(encoding="utf-8"))
            providers = raw if isinstance(raw, list) else (raw.get("providers") or [])
            active = next(
                (p for p in providers if isinstance(p, dict) and p.get("active")),
                None,
            )
            if active is None:
                return
            provider_name = active.get("name") or active.get("type") or "unknown"
            provider_type = (active.get("type") or "").lower().strip().replace("-", "_")

            # Providers that authenticate via stored token (OAuth / API key):
            # check offline and report immediately.
            # Local providers (lm_studio, ollama, openai_compat) don't require
            # auth credentials — treat them as "connected" on startup so the
            # banner doesn't stay at "connecting…" indefinitely.
            _LOCAL_PROVIDER_TYPES = {"lm_studio", "ollama", "openai_compat", "local"}

            if provider_type == "github_copilot":
                # Determine status from stored OAuth token (no network call).
                # Mirrors OpenCode's copilot.ts loader() which returns {} when no token.
                try:
                    mod = _load_copilot_auth_module()
                    initial_status = (
                        "connected" if mod.is_authenticated() else "disconnected"
                    )
                except Exception:
                    initial_status = "initializing"
            elif provider_type in _LOCAL_PROVIDER_TYPES or active.get("base_url"):
                # Local / self-hosted providers don't need an API key.
                # Report "connected" immediately; ProviderManager will overwrite
                # with "disconnected" if the endpoint is actually unreachable.
                initial_status = "connected"
            else:
                # Cloud providers with API keys: start at "initializing" and wait
                # for ProviderManager to probe the adapter.
                api_key = active.get("api_key") or ""
                initial_status = "connected" if api_key else "initializing"

            self._post(
                ProviderStatusChangeEvent(
                    provider=provider_name,
                    new_status=initial_status,
                    old_status="",
                )
            )

            # Also fire a ModelRoutingEvent with the first configured model so
            # the sidebar shows the model name on startup without waiting for an
            # agent run.  The real orchestrator will overwrite this with the
            # live-selected model once it initialises.
            if initial_status == "connected":
                try:
                    from src.ui.bus import ModelRoutingEvent

                    models = active.get("models") or []
                    startup_model = models[0] if models else ""
                    if startup_model:
                        self._post(
                            ModelRoutingEvent(
                                provider=provider_name,
                                model=startup_model,
                            )
                        )
                except Exception:
                    pass
        except Exception as exc:
            logger.debug(f"_publish_active_provider_status: {exc}")

    def _check_provider_auth_on_startup(self) -> None:
        """Post a warning if the active provider requires auth but has no token.

        Posts two messages:
          1. SessionHealthEvent — persistent inline banner in the chat panel
          2. NotificationEvent  — dismissible toast with actionable hint
        """
        try:
            from src.ui.bus import SessionHealthEvent, NotificationEvent
            import json, pathlib

            # Determine the active provider from providers.json
            providers_path = (
                pathlib.Path(__file__).parents[3] / "src" / "config" / "providers.json"
            )
            if not providers_path.exists():
                providers_path = pathlib.Path("src/config/providers.json")
            if not providers_path.exists():
                return
            raw = json.loads(providers_path.read_text(encoding="utf-8"))
            providers = raw if isinstance(raw, list) else raw.get("providers", [])
            active = next(
                (p for p in providers if isinstance(p, dict) and p.get("active")),
                None,
            )
            if active is None:
                return
            provider_type = active.get("type", "").lower()
            if provider_type != "github_copilot":
                return
            _copilot_mod = _load_copilot_auth_module()
            if not _copilot_mod.is_authenticated():
                # Persistent inline banner
                self._post(
                    SessionHealthEvent(
                        level="warning",
                        title="GitHub Copilot not connected",
                        message=(
                            "Open Settings (ctrl+s) → API Keys → "
                            "Login with GitHub Copilot to authenticate."
                        ),
                    )
                )
                # Also fire a dismissible toast
                self._post(
                    NotificationEvent(
                        level="warning",
                        message="GitHub Copilot: not connected — open Settings (ctrl+s) to log in.",
                    )
                )
        except Exception as exc:
            logger.debug(f"_check_provider_auth_on_startup: {exc}")

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

    # SUBAGENT-VIS-3: subagent lifecycle handlers

    def _on_delegation_start(self, payload: dict) -> None:
        from src.ui.bus import SubagentStartEvent

        self._post(
            SubagentStartEvent(
                child_session_id=payload.get("child_session_id", ""),
                role=payload.get("role", "unknown"),
                task=payload.get("task", ""),
                parent_session_id=payload.get("parent_session_id"),
            )
        )

    def _on_delegation_finish(self, payload: dict) -> None:
        from src.ui.bus import SubagentFinishEvent

        self._post(
            SubagentFinishEvent(
                child_session_id=payload.get("child_session_id", ""),
                role=payload.get("role", "unknown"),
                ok=payload.get("ok", True),
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

    def _get_active_context_length(self) -> int:
        """Return the active provider's context length for use as a token-budget
        fallback when the event payload does not include a limit field.

        Reads provider_context._load_active_context_length() which prefers the
        live value set by set_active_context_length() over the static providers.json
        entry.  Falls back to 32_768 if the import fails.
        """
        try:
            from src.core.inference.provider_context import (  # type: ignore[import]
                _load_active_context_length,
            )

            return _load_active_context_length()
        except Exception:
            return 32_768

    def _on_token_budget(self, payload: dict) -> None:
        from src.ui.bus import TokenBudgetEvent

        # Orchestrator publishes used_tokens/max_tokens; mock uses used/limit.
        used = payload.get("used") or payload.get("used_tokens", 0)
        _default_limit = self._get_active_context_length()
        limit = (
            payload.get("limit")
            or payload.get("max_tokens", _default_limit)
            or _default_limit
        )
        pct = payload.get("percent") or payload.get("usage_ratio", 0.0)
        # usage_ratio is 0..1, percent is 0..100 — normalise to 0..100
        if pct and pct <= 1.0:
            pct = pct * 100
        self._post(
            TokenBudgetEvent(
                used=int(used),
                limit=int(limit),
                percent=float(pct),
                warning=False,
            )
        )

    def _on_token_budget_warning(self, payload: dict) -> None:
        from src.ui.bus import TokenBudgetEvent

        used = payload.get("used") or payload.get("used_tokens", 0)
        _default_limit = self._get_active_context_length()
        limit = (
            payload.get("limit")
            or payload.get("max_tokens", _default_limit)
            or _default_limit
        )
        pct = payload.get("percent") or payload.get("usage_ratio", 0.0)
        if pct and pct <= 1.0:
            pct = pct * 100
        self._post(
            TokenBudgetEvent(
                used=int(used),
                limit=int(limit),
                percent=float(pct),
                warning=True,
            )
        )

    def _on_provider_context_window(self, payload: dict) -> None:
        """Update the TUI's context_window reactive when the provider reports
        the actual loaded context length (e.g. LM Studio /api/v0/models)."""
        ctx = payload.get("context_window", 0)
        if ctx and ctx > 0:
            # LIVE-CTX: Propagate the live context length to provider_context so
            # get_context_budget() (and perception_node) use the real value
            # fetched from the models endpoint rather than the static file value.
            try:
                from src.core.inference.provider_context import (
                    set_active_context_length,
                )  # type: ignore[import]

                set_active_context_length(int(ctx))
            except Exception:
                pass
            try:
                from src.ui.events import UpdateSettings

                self._post(UpdateSettings(updates={"context_window": int(ctx)}))
            except Exception:
                pass
            # Also propagate to the token_monitor so the budget limit reflects
            # the real model context window in subsequent token.budget events.
            # Update BOTH the "default" session (used before any task starts) AND
            # the live task session_id so whichever key the orchestrator queries,
            # get_budget(...).max_tokens returns the correct value.
            try:
                from src.core.orchestration.token_budget import get_token_budget_monitor  # type: ignore[import]

                monitor = get_token_budget_monitor()
                # Always seed the default session
                monitor.update(session_id="default", used_tokens=0, max_tokens=int(ctx))
                # Also update the current task session if one is active
                live_session_id = getattr(self._orchestrator, "_current_task_id", None)
                if live_session_id and live_session_id != "default":
                    monitor.update(
                        session_id=live_session_id, used_tokens=0, max_tokens=int(ctx)
                    )
            except Exception:
                pass

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
                has_error=payload.get("has_error", False),
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

    def _on_subagent_cost(self, payload: dict) -> None:
        """GAP-NEW-7: accumulate child session cost into the parent session total.

        Publishes a UsageTurnSummaryEvent with 0 tokens so the cost panel
        reflects subagent spend without creating a phantom model turn.
        """
        from src.ui.bus import UsageTurnSummaryEvent

        child_cost = float(payload.get("cost_usd", 0.0))
        if child_cost <= 0:
            return
        role = str(payload.get("role", "subagent"))
        self._post(
            UsageTurnSummaryEvent(
                input_tokens=0,
                output_tokens=0,
                model=f"[{role}]",
                cost_usd=child_cost,
            )
        )

    def _on_agent_message(self, payload: dict) -> None:
        """CP-15: route send_user_message bus events to the chat panel.

        ``send_user_message`` publishes ``agent.message`` with keys:
          message, attachments, status ("normal" | "proactive").
        Route as AgentFinalResponse so the chat panel renders it immediately.
        """
        from src.ui.bus import AgentFinalResponse

        text = payload.get("message", "")
        if text:
            self._post(AgentFinalResponse(content=text))

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
                # NOTE: start_new_task() is intentionally NOT called here — it
                # clears msg_mgr.messages, which would wipe conversation history
                # on every follow-up message.  start_new_task() is called only
                # from start_new_session() (triggered by /new).
                # AUTO-02: apply per-role autonomy settings before each run
                try:
                    from src.core.config_loader import (
                        get_role_config,
                        load_merged_config,
                    )
                    from src.tools.tools_config import (
                        set_autonomous,
                        set_require_preview_confirmation,
                    )  # type: ignore[import]

                    _wdir = Path(self._working_dir) if self._working_dir else None
                    role_cfg = get_role_config(self._active_role, working_dir=_wdir)
                    set_autonomous(bool(role_cfg.get("autonomous", False)))
                    # PREV-1: Apply preview_confirmation flag from workspace config
                    _merged_cfg = load_merged_config(_wdir)
                    set_require_preview_confirmation(
                        bool(_merged_cfg.get("preview_confirmation", False))
                    )
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
                # Append the user message BEFORE reading the list so that
                # run_agent_once sees it as messages[-1] and sets task=text.
                if msg_mgr is not None:
                    msg_mgr.append("user", text)
                    messages = list(getattr(msg_mgr, "messages", []))
                elif callable(getattr(self._orchestrator, "get_messages", None)):
                    messages = list(self._orchestrator.get_messages())
                    messages.append({"role": "user", "content": text})
                else:
                    messages = [{"role": "user", "content": text}]
                tools = self._orchestrator.get_tools_for_role("operational")
                result = self._orchestrator.run_agent_once(
                    system_prompt_name=prompt_name,
                    messages=messages,
                    tools=tools,
                    cancel_event=self._cancel_event,
                )
                self._orchestrator.flush_execution_trace()
                # run_agent_once() returns {"assistant_message": ..., "work_summary": ...}
                # Keep fallbacks for "response" and "last_result" for backwards compat.
                content = (
                    result.get("assistant_message")
                    or result.get("response")
                    or (result.get("last_result") or {}).get("output", "")
                )
                if content:
                    with self._history_lock:
                        self.history.append(("assistant", content))
                    self._save_history()
                    self._post(AgentFinalResponse(content=content))
                # §10.4 — capture state for /continue.  Write via
                # call_from_thread so the Textual event-loop thread owns the
                # assignment and there is no data race.
                _r = result
                self.app.call_from_thread(
                    lambda r=_r: setattr(self.app, "_continue_state", r)
                )
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

    def confirm_file_preview(self, path: str) -> None:
        """User accepted the diff preview for a file write — resolve the gate.

        Publishes ``preview.confirmed`` with a ``path`` key so that
        ``PreviewCoordinator._on_confirmed`` can resolve the threading.Event
        gate registered in ``file_tools.register_preview_gate()``.
        """
        self._bus.publish("preview.confirmed", {"path": path})

    def reject_file_preview(self, path: str) -> None:
        """User rejected the diff preview for a file write — resolve the gate.

        Publishes ``preview.rejected`` with a ``path`` key so that
        ``PreviewCoordinator._on_rejected`` can set the rejected flag in
        ``file_tools._preview_rejected`` and unblock the gate.
        """
        self._bus.publish("preview.rejected", {"path": path})

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
        """Publish session.request_state on startup (§10.1 step 6).

        Also triggers the full startup chain via ``_publish_system_settings()``:
        loads config, fires ``orchestrator.startup`` → ``_on_orchestrator_startup``
        → ``_publish_active_provider_status()`` so the banner and sidebar reflect
        the real provider state immediately instead of staying at
        "connecting…" / "disconnected".
        """
        self._bus.publish("session.request_state", {"session_id": "default"})
        # Kick off the startup chain so the UI status indicators are updated.
        self._publish_system_settings()

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
