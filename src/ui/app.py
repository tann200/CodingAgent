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

from src.core.llm_manager import get_provider_manager
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
        # ensure provider manager uses our bus
        pm = get_provider_manager()
        pm.set_event_bus(self.event_bus)

        # Wire GUILogger to EventBus for real-time log forwarding
        from src.core.logger import logger as _guilogger
        from src.core.orchestration.event_bus import get_event_bus

        try:
            get_event_bus().subscribe(
                "log.new",
                lambda payload: _guilogger.log(
                    str(payload.get("message", "")), payload.get("level", "INFO")
                ),
            )
        except Exception:
            pass

        # instantiate orchestrator with the shared event bus and message window
        self.orchestrator = Orchestrator(
            working_dir=self.config.working_dir, allow_external_working_dir=True
        )
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
        guilogger.info("CodingAgentApp initialized")

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
                            app.run()
                            return
                    except Exception as e:
                        guilogger.error(f"Failed to start Textual app: {e}")
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
        try:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            p = os.path.join(root, "tmp_app_started.log")
            with open(p, "w", encoding="utf-8") as f:
                f.write("started")
        except Exception:
            guilogger.debug("Failed to write tmp_app_started.log")
