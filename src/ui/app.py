"""Main TUI application wrapper.

This module provides an application class `CodingAgentApp` that wires the core
services into the TUI: EventBus, Orchestrator, ProviderManager. For now it's a
lightweight shim that can be constructed in tests without launching any UI.

The real Textual app will be implemented later and will subclass `CodingAgentApp`
or integrate it as a controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import os
from pathlib import Path

from src.core.inference.llm_manager import get_provider_manager
import src.core.orchestration.event_bus as _event_bus_module
from src.core.orchestration.event_bus import EventBus
from src.core.orchestration.orchestrator import Orchestrator
from src.core.logger import logger as guilogger


try:
    from src.core.telemetry.consumer import TelemetryConsumer
except Exception:
    TelemetryConsumer = None


@dataclass
class AppConfig:
    working_dir: Optional[str] = None
    debug: bool = False
    telemetry_enabled: bool = False
    telemetry_path: Optional[str] = None


class CodingAgentApp:
    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()

        # Install centralized logging handler early - Textual will consume logs via EventBus
        from src.core.logger import install_stdlib_handler

        install_stdlib_handler()

        self.event_bus = EventBus()
        # Wire get_event_bus() singleton to this instance so TUI handlers and
        # the orchestrator share the same bus (singleton split fix).
        _event_bus_module._default_bus = self.event_bus
        # ensure provider manager uses our bus
        pm = get_provider_manager()
        pm.set_event_bus(self.event_bus)

        # GUILogger already publishes log.new events; subscribing back to log.new
        # and calling _guilogger.log() would create a recursive publish loop.
        # The TUI consumes log.new directly via EventBus subscriptions in the view layer.

        # instantiate orchestrator with the shared event bus and message window
        self.orchestrator = Orchestrator(
            working_dir=self.config.working_dir, allow_external_working_dir=True
        )

        # Start session health monitoring
        try:
            from src.core.orchestration.session_watcher import get_session_watcher
            from src.core.orchestration.session_registry import get_session_registry

            watcher = get_session_watcher()
            watcher.start()

            registry = get_session_registry()
            registry.start_health_monitor()

            # Wire alerts to event bus for UI notification
            def _on_session_alert(alert):
                self.event_bus.publish(
                    "ui.notification",
                    {
                        "level": "warning"
                        if alert.level.value in ("warning", "info")
                        else "error",
                        "message": alert.title,
                        "source": "session_watcher",
                    },
                )

            watcher.on_alert(_on_session_alert)
            guilogger.info("Session health monitoring started")
        except Exception as e:
            guilogger.warning(f"Failed to start session watcher (non-fatal): {e}")

        # Wire telemetry consumer if requested
        try:
            if (
                getattr(self.config, "telemetry_enabled", False)
                and TelemetryConsumer is not None
            ):
                telemetry_path = self.config.telemetry_path or (
                    Path(self.config.working_dir or ".") / "telemetry.jsonl"
                )
                try:
                    self.telemetry_consumer = TelemetryConsumer(
                        self.event_bus, Path(telemetry_path)
                    )
                    # also instantiate in-memory metrics collector for quick inspection
                    try:
                        from src.core.telemetry.metrics import TelemetryMetrics

                        self.telemetry_metrics = TelemetryMetrics(self.event_bus)
                    except Exception:
                        self.telemetry_metrics = None
                    guilogger.info(f"Telemetry consumer enabled at {telemetry_path}")
                except Exception:
                    guilogger.exception("Failed to initialize TelemetryConsumer")
        except Exception:
            pass
        # W11: Run provider health check at startup — warn immediately if no provider is reachable
        try:
            from src.core.startup import run_provider_health_check_sync

            health = run_provider_health_check_sync(timeout=5.0)
            reachable = [k for k, v in health.items() if not v.get("error")]
            if reachable:
                guilogger.info(
                    f"Provider health check: {len(reachable)} provider(s) reachable: {reachable}"
                )
            else:
                guilogger.warning(
                    "Provider health check: NO providers reachable. "
                    "Start LM Studio or Ollama before sending tasks. "
                    f"Results: {health}"
                )
                self.event_bus.publish(
                    "ui.notification",
                    {
                        "level": "warning",
                        "message": "No LLM providers reachable. Start LM Studio or Ollama.",
                        "source": "startup",
                    },
                )
        except Exception as e:
            guilogger.warning(f"Provider health check failed (non-fatal): {e}")

        guilogger.info("CodingAgentApp initialized")

    def shutdown(self) -> None:
        """Clean up resources on app shutdown."""
        try:
            from src.core.orchestration.session_watcher import get_session_watcher
            from src.core.orchestration.session_registry import get_session_registry
            from src.core.orchestration.cross_session_bus import get_cross_session_bus

            watcher = get_session_watcher()
            watcher.stop()

            registry = get_session_registry()
            registry.shutdown()

            bus = get_cross_session_bus()
            bus.shutdown()

            guilogger.info("Session monitoring shutdown complete")
        except Exception as e:
            guilogger.warning(f"Error during session monitoring shutdown: {e}")

    def run(self) -> None:
        """Start the TUI or headless app.

        Behavior:
        - If environment variable ENABLE_TUI is set to '1'/'true' and the
          `textual` package is available, instantiate and run the Textual app.
        - Otherwise run in headless mode (useful for tests/CI).
        """
        guilogger.info("CodingAgentApp.run called")

        enable_tui_env = os.getenv("ENABLE_TUI")
        enable_tui = False
        if enable_tui_env and str(enable_tui_env).lower() in ("1", "true", "yes"):
            enable_tui = True

        if enable_tui:
            # try textual
            try:
                import importlib.util as _il

                if _il.find_spec("textual") is not None:
                    guilogger.info(
                        "Textual detected and ENABLE_TUI requested; launching TUI"
                    )
                    try:
                        from src.ui.textual_app_impl import create_app

                        app = create_app(orchestrator=self.orchestrator)
                        if hasattr(app, "run") and callable(getattr(app, "run")):
                            try:
                                app.run()
                            finally:
                                self.shutdown()
                            return
                    except Exception as e:
                        guilogger.error(f"Failed to start Textual app: {e}")
                        self.shutdown()
                        return
                else:
                    guilogger.info(
                        "Textual not found in environment; falling back to headless"
                    )
            except Exception:
                guilogger.exception("Error while checking for textual")

        # headless fallback
        guilogger.info("Starting headless mode (no UI)")
        try:
            print("CodingAgentApp started (headless mode)", flush=True)
        except Exception:
            pass
        finally:
            self.shutdown()
