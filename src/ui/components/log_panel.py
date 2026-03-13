"""Log panel component that subscribes to GUILogger queue via EventBus.

This is a testable controller that collects logs and exposes them to the UI.
"""
from __future__ import annotations

from typing import List
from src.core.orchestration.event_bus import EventBus


class LogPanel:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.entries: List[dict] = []
        # subscribe to a hypothetical 'log.new' event
        self.event_bus.subscribe('log.new', self._on_new_log)

    def _on_new_log(self, payload):
        try:
            self.entries.append(payload)
        except Exception:
            pass

    def tail(self, n: int = 100) -> List[dict]:
        return self.entries[-n:]

