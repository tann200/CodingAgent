"""Minimal thread-safe in-process EventBus used for telemetry and UI notifications.

API:
- EventBus.subscribe(event_name: str, callback: Callable[[Any], None]) -> None
- EventBus.unsubscribe(event_name: str, callback: Callable[[Any], None]) -> None
- EventBus.publish(event_name: str, payload: Any, correlation_id: str | None) -> None
- EventBus.subscribe_to_agent(agent_id: str, callback) -> None
- EventBus.publish_to_agent(agent_id: str, payload: Any) -> None

Callbacks are executed synchronously in the publisher's thread. Subscriber exceptions are
caught and ignored to keep the event bus robust for telemetry.

Correlation IDs (#26):
- Call ``new_correlation_id()`` at the start of each agent turn to mint and set a UUID.
- All subsequent ``publish()`` calls in that context automatically stamp dict payloads
  with ``_correlation_id``, allowing downstream logging to correlate related events.
- Use ``get_correlation_id()`` / ``set_correlation_id()`` to read/write the current ID.
"""

from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import IntEnum

# ---------------------------------------------------------------------------
# Correlation-ID context variable — set this at the start of each agent turn
# so every EventBus.publish() and LLM call within that turn shares the same ID.
# ---------------------------------------------------------------------------
_current_correlation_id: ContextVar[Optional[str]] = ContextVar(
    "_current_correlation_id", default=None
)


def set_correlation_id(correlation_id: str) -> None:
    """Set the correlation ID for the current async/thread context."""
    _current_correlation_id.set(correlation_id)


def get_correlation_id() -> Optional[str]:
    """Return the current correlation ID, or None if not set."""
    return _current_correlation_id.get()


def new_correlation_id() -> str:
    """Generate a new UUID4 correlation ID and set it in the current context."""
    cid = str(uuid.uuid4())
    _current_correlation_id.set(cid)
    return cid


class MessagePriority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class AgentMessage:
    agent_id: str
    payload: Any
    priority: MessagePriority = MessagePriority.NORMAL
    reply_to: Optional[str] = None
    timestamp: float = field(default_factory=lambda: __import__("time").time())


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[Callable[[Any], None]]] = {}
        self._agent_subscribers: Dict[str, List[Callable[[AgentMessage], None]]] = {}
        self._agent_ids: Set[str] = set()

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

    def publish(
        self,
        event_name: str,
        payload: Any,
        correlation_id: Optional[str] = None,
    ) -> None:
        """Publish an event.

        If *payload* is a dict and does not already contain ``_correlation_id``,
        the current context correlation ID (or the supplied *correlation_id*) is
        injected automatically so every subscriber can trace the event.
        """
        cid = correlation_id or _current_correlation_id.get()
        if cid is not None and isinstance(payload, dict) and "_correlation_id" not in payload:
            payload = {**payload, "_correlation_id": cid}
        with self._lock:
            subs = list(self._subscribers.get(event_name, []))
        for cb in subs:
            try:
                cb(payload)
            except Exception:
                continue

    def subscribe_to_agent(
        self, agent_id: str, callback: Callable[[AgentMessage], None]
    ) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            self._agent_ids.add(agent_id)
            self._agent_subscribers.setdefault(agent_id, []).append(callback)

    def unsubscribe_from_agent(
        self, agent_id: str, callback: Callable[[AgentMessage], None]
    ) -> None:
        with self._lock:
            if agent_id in self._agent_subscribers:
                try:
                    self._agent_subscribers[agent_id].remove(callback)
                except ValueError:
                    pass

    def publish_to_agent(
        self,
        agent_id: str,
        payload: Any,
        priority: MessagePriority = MessagePriority.NORMAL,
        reply_to: Optional[str] = None,
    ) -> None:
        msg = AgentMessage(
            agent_id=agent_id, payload=payload, priority=priority, reply_to=reply_to
        )
        with self._lock:
            subs = list(self._agent_subscribers.get(agent_id, []))
            priority_subs = list(self._agent_subscribers.get("*", []))
        for cb in priority_subs:
            try:
                cb(msg)
            except Exception:
                continue
        if priority >= MessagePriority.HIGH:
            for cb in sorted(subs, key=lambda x: 0):
                try:
                    cb(msg)
                except Exception:
                    continue
        else:
            for cb in subs:
                try:
                    cb(msg)
                except Exception:
                    continue

    def broadcast_to_agents(
        self, payload: Any, priority: MessagePriority = MessagePriority.NORMAL
    ) -> None:
        msg = AgentMessage(agent_id="broadcast", payload=payload, priority=priority)
        with self._lock:
            all_subs = []
            for agent_id in self._agent_ids:
                all_subs.extend(self._agent_subscribers.get(agent_id, []))
            all_subs.extend(self._agent_subscribers.get("*", []))
        for cb in all_subs:
            try:
                cb(msg)
            except Exception:
                continue

    def list_registered_agents(self) -> List[str]:
        with self._lock:
            return list(self._agent_ids)

    def publish_with_identity(
        self,
        event_name: str,
        payload: Any,
        sender_id: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> None:
        meta = {"sender_id": sender_id, "priority": priority}
        full = {"meta": meta, "payload": payload}
        return self.publish(event_name, full)


_default_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus
