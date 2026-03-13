"""Minimal thread-safe in-process EventBus used for telemetry and UI notifications.

API:
- EventBus.subscribe(event_name: str, callback: Callable[[Any], None]) -> None
- EventBus.unsubscribe(event_name: str, callback: Callable[[Any], None]) -> None
- EventBus.publish(event_name: str, payload: Any) -> None

Callbacks are executed synchronously in the publisher's thread. Subscriber exceptions are
caught and ignored to keep the event bus robust for telemetry.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[Callable[[Any], None]]] = {}

    def subscribe(self, event_name: str, callback: Callable[[Any], None]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            self._subscribers.setdefault(event_name, []).append(callback)

    def unsubscribe(self, event_name: str, callback: Callable[[Any], None]) -> None:
        with self._lock:
            if event_name in self._subscribers:
                try:
                    self._subscribers[event_name].remove(callback)
                except ValueError:
                    pass

    def publish(self, event_name: str, payload: Any) -> None:
        # Copy subscribers under lock to avoid holding lock while invoking callbacks
        with self._lock:
            subs = list(self._subscribers.get(event_name, []))
        for cb in subs:
            try:
                cb(payload)
            except Exception:
                # Do not let subscriber exceptions break the publisher
                continue


# Optional singleton accessor for convenience
_default_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus
