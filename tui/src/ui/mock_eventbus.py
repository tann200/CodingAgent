"""
Simple string-event EventBus for mock/dev mode.
API surface matches src.core.orchestration.event_bus.EventBus so the
bridge works identically against real and mock backends.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Callable


class MockEventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event: str, callback: Callable) -> None:
        with self._lock:
            if callback not in self._subscribers[event]:
                self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        with self._lock:
            try:
                self._subscribers[event].remove(callback)
            except ValueError:
                pass

    def publish(self, event: str, payload: dict | None = None) -> None:
        if payload is None:
            payload = {}
        with self._lock:
            callbacks = list(self._subscribers.get(event, []))
        for cb in callbacks:
            try:
                cb(payload)
            except Exception:
                pass


_mock_bus: MockEventBus | None = None
_bus_lock = threading.Lock()


def get_mock_event_bus() -> MockEventBus:
    global _mock_bus
    with _bus_lock:
        if _mock_bus is None:
            _mock_bus = MockEventBus()
        return _mock_bus


def reset_mock_event_bus() -> None:
    global _mock_bus
    with _bus_lock:
        _mock_bus = None
