"""Main view controller for the TUI.

This module exposes a `MainViewController` class that will be used by the
Textual application to build the layout. For tests it can be instantiated and
inspected without rendering.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.core.orchestration.event_bus import EventBus


@dataclass
class MainViewState:
    status: str = "idle"
    active_provider: Optional[str] = None


class MainViewController:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.state = MainViewState()
        # subscribe to orchestrator/provider events
        self.event_bus.subscribe("orchestrator.startup", self._on_startup)
        self.event_bus.subscribe("provider.status.changed", self._on_provider_status)

    def _on_startup(self, payload):
        self.state.status = "started"

    def _on_provider_status(self, payload):
        if isinstance(payload, dict):
            self.state.active_provider = payload.get("provider")

    def get_status(self) -> str:
        return self.state.status

