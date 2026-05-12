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

import asyncio
import atexit
import contextvars
import functools
import inspect
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar, Awaitable

from dataclasses import dataclass, field
from enum import IntEnum

_logger = logging.getLogger(__name__)

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


T = TypeVar("T")


def run_with_correlation(
    loop: "asyncio.AbstractEventLoop",
    executor: Optional[Any],
    fn: Callable[..., T],
    *args: Any,
) -> Awaitable[T]:
    """D-07: Run *fn* in *executor* with the current correlation ID propagated.

    ``loop.run_in_executor(None, fn, *args)`` does NOT propagate ContextVar
    values (including the correlation ID) into the worker thread.  This helper
    copies the current context and runs the callable inside it so that log
    records and EventBus publishes inside the thread carry the same ID.

    Usage::

        from src.core.orchestration.event_bus import run_with_correlation

        result = await run_with_correlation(loop, None, my_sync_fn, arg1, arg2)
    """
    # Local imports to avoid import-time cycles and keep the function test-friendly.
    ctx = contextvars.copy_context()

    # Wrap the user's callable so that if it returns an awaitable (a coroutine
    # or other awaitable), we execute it to completion inside the worker thread
    # using asyncio.run. This prevents coroutine objects from leaking back to
    # the caller's event loop.
    def _worker() -> T:  # zero-arg callable for run_in_executor
        rv = fn(*args)
        try:
            if inspect.isawaitable(rv):
                # Run awaitable to completion inside this worker thread.
                return asyncio.run(rv)  # type: ignore[return-value]
        except Exception:
            # If anything goes wrong executing the awaitable, propagate the
            # exception to the caller via the Future returned by run_in_executor.
            raise
        return rv  # type: ignore[return-value]

    # Use a shared long-lived executor when the caller didn't provide one. This
    # reduces thread creation overhead and makes shutdown explicit via atexit.
    sel_executor = executor if executor is not None else _get_shared_executor()

    # Use functools.partial so a single callable is passed to run_in_executor.
    fn_partial = functools.partial(ctx.run, _worker)
    # loop.run_in_executor returns an awaitable. Typing uses a TypeVar T so callers
    # receive a properly-parameterised Awaitable[T] when they pass a typed fn.
    return loop.run_in_executor(sel_executor, fn_partial)


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
    timestamp: float = field(default_factory=time.time)


@dataclass
class DispatchEvent:
    """Event for subagent dispatch (similar to OpenClaw's DispatchEvent)."""

    session_id: str
    agent_id: str
    task: str
    parent_session_id: Optional[str] = None
    context: str = ""
    correlation_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class DispatchResultEvent:
    """Event for subagent dispatch result (similar to OpenClaw's DispatchResultEvent)."""

    session_id: str
    content: str = ""
    parent_session_id: Optional[str] = None
    error: Optional[str] = None
    correlation_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


# Event names for dispatch (matching OpenClaw patterns)
class DispatchEvents:
    SUBAGENT_DISPATCH = "subagent.dispatch"
    SUBAGENT_RESULT = "subagent.result"
    SUBAGENT_TIMEOUT = "subagent.timeout"
    SUBAGENT_FAILED = "subagent.failed"
    SUBAGENT_SPAWN = "subagent.spawn"
    SUBAGENT_COMPLETE = "subagent.complete"


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

    def has_subscribers(self, event_name: str) -> bool:
        """Return True if any subscriber is registered for event_name."""
        with self._lock:
            return bool(self._subscribers.get(event_name))

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
        if (
            cid is not None
            and isinstance(payload, dict)
            and "_correlation_id" not in payload
        ):
            payload = {**payload, "_correlation_id": cid}
        with self._lock:
            subs = list(self._subscribers.get(event_name, []))
        for cb in subs:
            try:
                cb(payload)
            except Exception as _exc:
                _logger.warning(
                    "EventBus: subscriber %r raised on event %r: %s",
                    cb,
                    event_name,
                    _exc,
                )
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
            wildcard_subs = list(self._agent_subscribers.get("*", []))
        # C5 fix: deduplicate so callbacks registered on both "*" and the specific
        # agent_id are invoked exactly once (wildcard first, then specific-only).
        called: set = set()
        for cb in wildcard_subs:
            called.add(id(cb))
            try:
                cb(msg)
            except Exception:
                continue
        for cb in subs:
            if id(cb) in called:
                continue
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

    # ---------------------------------------------------------------------------
    # Subagent dispatch helpers (matching OpenClaw patterns)
    # ---------------------------------------------------------------------------

    def publish_dispatch(self, event: DispatchEvent) -> None:
        """Publish a subagent dispatch event."""
        self.publish(
            DispatchEvents.SUBAGENT_DISPATCH,
            {
                "session_id": event.session_id,
                "parent_session_id": event.parent_session_id,
                "agent_id": event.agent_id,
                "task": event.task,
                "context": event.context,
            },
            correlation_id=event.correlation_id,
        )

    def publish_dispatch_result(self, event: DispatchResultEvent) -> None:
        """Publish a subagent dispatch result event."""
        self.publish(
            DispatchEvents.SUBAGENT_RESULT,
            {
                "session_id": event.session_id,
                "parent_session_id": event.parent_session_id,
                "content": event.content,
                "error": event.error,
            },
            correlation_id=event.correlation_id,
        )

    def subscribe_dispatch(self, callback: Callable[[DispatchEvent], None]) -> None:
        """Subscribe to subagent dispatch events."""
        self.subscribe(DispatchEvents.SUBAGENT_DISPATCH, callback)

    def subscribe_dispatch_result(
        self, callback: Callable[[DispatchResultEvent], None]
    ) -> None:
        """Subscribe to subagent dispatch result events."""
        self.subscribe(DispatchEvents.SUBAGENT_RESULT, callback)

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

    def subscribe_to_topic(
        self, topic: str, callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """Subscribe to a topic (e.g., 'agent.scout.broadcast')."""
        self.subscribe(f"topic.{topic}", callback)

    def publish_to_topic(
        self, topic: str, payload: Dict[str, Any], sender_id: Optional[str] = None
    ) -> None:
        """Publish to a topic."""
        full_topic = f"topic.{topic}"

        if sender_id:
            payload = {**payload, "sender_id": sender_id}

        self.publish(full_topic, payload)

    def subscribe_to_preview_complete(
        self, callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """Subscribe to preview confirmed/rejected events."""
        self.subscribe("preview.confirmed", callback)
        self.subscribe("preview.rejected", callback)


_default_bus: EventBus | None = None
_bus_lock = threading.Lock()

# Shared executor for worker threads. Lazily created to avoid import-time side-effects.
_shared_executor = None  # type: ignore[assignment]


def _get_shared_executor():
    """Return a long-lived ThreadPoolExecutor for run_in_executor calls.

    Lazily creates the executor and registers an atexit shutdown handler so
    worker threads are cleaned up on process exit.
    """
    global _shared_executor
    if _shared_executor is not None:
        return _shared_executor
    _shared_executor = ThreadPoolExecutor(thread_name_prefix="coding_agent_worker")
    atexit.register(_shared_executor.shutdown, wait=True)
    return _shared_executor


def get_event_bus() -> EventBus:
    """Return the process-wide default EventBus, creating it on first call.

    Uses a double-checked lock so the singleton is safe under concurrent
    initialisation from multiple threads (e.g. executor threads starting before
    the main thread has completed setup).
    """
    global _default_bus
    if _default_bus is None:
        with _bus_lock:
            if _default_bus is None:  # double-checked lock
                _default_bus = EventBus()
    return _default_bus


# P2-T5: safety events that must always be visible, even in headless/CLI mode.
_SAFETY_EVENTS: frozenset = frozenset(
    {"system.warning", "tool.doom_loop_detected", "system.error"}
)


def subscribe_stderr_fallback(event_bus: "EventBus") -> None:
    """Register a stderr fallback subscriber for safety events.

    Call from Orchestrator.__init__ or the CLI entry point so headless runs
    always surface sandbox-degradation and doom-loop warnings even when no
    TUI subscriber is active.

    Note: when a TUI IS active it will have its own subscriber; this fallback
    will ALSO fire, resulting in duplicate output (once in the TUI, once to
    stderr).  This is intentional — safety warnings should never be silent.
    """
    import sys as _sys

    def _stderr_handler(payload: Any) -> None:
        msg = payload.get("message") if isinstance(payload, dict) else str(payload)
        print(f"[CodingAgent WARNING] {msg}", file=_sys.stderr, flush=True)

    for ev in _SAFETY_EVENTS:
        event_bus.subscribe(ev, _stderr_handler)
