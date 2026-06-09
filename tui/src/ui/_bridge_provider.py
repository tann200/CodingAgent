"""BridgeProviderMixin — provider/model event handlers and startup publishing.

Contains: _publish_system_settings, _publish_active_provider_status,
_check_provider_auth_on_startup, _on_orchestrator_startup, _on_system_settings,
_on_provider_status, _on_models_list, _on_model_routing, _on_model_response,
_on_model_token, _on_stream_chunk, _on_provider_context_window, get_fast_model.
"""

from __future__ import annotations


from src.core.messaging.event_types import OrchestratorStartup, SystemSettings
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from ._bridge_protocol import AgentBridgeProtocol
from .logging import get_logger

logger = get_logger("bridge")


class BridgeProviderMixin(AgentBridgeProtocol):
    """Mixin providing provider and model event handlers."""

    def _publish_system_settings(self) -> None:
        """TUI-11: Respond to RequestSystemSettings() from AgentApp.on_mount().

        Publishes a ``system.settings`` event that AgentApp converts to a
        ``SystemSettingsLoaded`` Textual message.  Falls back gracefully when
        the orchestrator or config are unavailable (dev / mock mode).
        """
        try:
            from src.core.config_loader import load_merged_config  # type: ignore[import]

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
            self._bus.publish_typed(SystemSettings(active_mode=cfg.get("active_mode", "lead_architect"), theme=cfg.get("theme", "textual-dark"), context_window=_context_window, default_provider=cfg.get("default_provider", "none"), default_model=cfg.get("default_model", "none"), providers=providers, autonomous_mode=cfg.get("autonomous_mode", False), max_turns=cfg.get("max_turns", 50)))
        except Exception as exc:
            logger.debug(f"_publish_system_settings: {exc}")

        # Also publish startup / running events so UI banners reflect state
        try:
            self._bus.publish_typed(OrchestratorStartup(working_dir=self._working_dir or str(Path.cwd())))
        except Exception:
            pass
        # The orchestrator startup subscription is now direct (the old
        # _EVENT_MAP remapping has been removed), so the bus event will be
        # received normally.  We also eagerly publish active provider status
        # here for immediate UI feedback.
        self._publish_active_provider_status()
        self._check_provider_auth_on_startup()
        try:
            from tui.tui_src.ui.bus import AgentRunningEvent

            self._post(AgentRunningEvent(running=False))
        except Exception:
            pass

    def _publish_active_provider_status(self) -> None:
        """Read providers.json and immediately fire a provider status event for
        the active provider so the banner shows the real name on startup.

        For GitHub Copilot (which uses OAuth device flow, not a network probe),
        we call validate_connection() / is_authenticated() synchronously so the
        banner immediately reflects real auth state instead of staying at
        "initializing…" until ProviderManager.initialize() completes.
        """
        try:
            from tui.tui_src.ui.bus import ProviderStatusChangeEvent
            import json
            import pathlib

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
                    from tui.tui_src.ui.core_bridge import _load_copilot_auth_module
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
                    from tui.tui_src.ui.bus import ModelRoutingEvent

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
            from tui.tui_src.ui.bus import SessionHealthEvent, NotificationEvent
            import json
            import pathlib

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
            from tui.tui_src.ui.core_bridge import _load_copilot_auth_module
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

    def _on_orchestrator_startup(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import OrchestratorReadyEvent

        wd = payload.get("working_dir", "")
        self._working_dir = wd
        self._post(OrchestratorReadyEvent(working_dir=wd))

        # Immediately publish the active provider so the banner shows the real name
        # before the slow async ProviderManager.initialize() completes.
        self._publish_active_provider_status()

        # Warn the user if the active provider is GitHub Copilot but has no token.
        self._check_provider_auth_on_startup()

    def _on_system_settings(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import SystemSettingsLoaded

        providers = payload.get("providers")
        if not isinstance(providers, list):
            providers = []

        settings = {
            "active_mode": payload.get("active_mode", "lead_architect"),
            "theme": payload.get("theme", "textual-dark"),
            "context_window": payload.get("context_window", 32768),
            "default_provider": payload.get("default_provider", "none"),
            "default_model": payload.get("default_model", "none"),
            "autonomous_mode": payload.get("autonomous_mode", False),
            "max_turns": payload.get("max_turns", 50),
        }
        self._post(
            SystemSettingsLoaded(
                settings_dict=settings,
                available_providers=providers,
            )
        )

    def _on_provider_status(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import ProviderStatusChangeEvent

        self._post(
            ProviderStatusChangeEvent(
                provider=payload.get("provider", ""),
                new_status=payload.get("status", ""),
                old_status="",
            )
        )

    def _on_provider_unavailable(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import NotificationEvent

        reason = payload.get("reason", "Provider unavailable")
        self._post(
            NotificationEvent(
                level="warning",
                message=f"[bold yellow]⚠ Provider unavailable:[/] {reason} [dim]— run /provider to try again[/]",
            )
        )

    def _on_models_list(self, payload: dict) -> None:
        logger.debug(
            f"Models: {payload.get('provider')} — {len(payload.get('models', []))} models"
        )

    def _on_model_routing(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import ModelRoutingEvent

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
        from tui.tui_src.ui.bus import StreamChunkEvent

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
                from tui.tui_src.ui.bus import DisplayReasoning

                self._post(DisplayReasoning(content=chunk, start_time=_time.time()))
            except ImportError:
                # DisplayReasoning may not exist in all TUI versions; fall back silently
                pass
        else:
            from tui.tui_src.ui.bus import StreamChunkEvent

            self._post(StreamChunkEvent(chunk=chunk, is_partial=True))

    def _on_provider_context_window(self, payload: dict) -> None:
        """Update the TUI's context_window reactive when the provider reports
        the actual loaded context length (e.g. LM Studio /api/v0/models)."""
        ctx = payload.get("context_window", 0)
        if ctx and ctx > 0:
            provider_id = payload.get("provider", "")
            try:
                from src.core.inference.provider_context import (  # type: ignore[import]
                    set_active_context_length,
                )

                set_active_context_length(int(ctx), provider_id=provider_id)
            except Exception:
                pass
            try:
                from tui.tui_src.ui.events import UpdateSettings

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

    def get_fast_model(self) -> dict:
        """S8-C: Return the NANO-tier model from config (for /fast command).

        Checks ``model_routing.nano_model`` in the loaded config.  Returns a
        dict with a ``model`` key when found; empty dict otherwise.
        """
        try:
            from src.core.config_loader import load_merged_config  # type: ignore[import]

            cfg = load_merged_config()
            nano_model = (cfg.get("model_routing") or {}).get("nano_model")
            if nano_model:
                return {"model": nano_model, "tier": "nano"}
        except Exception:
            pass
        return {}
