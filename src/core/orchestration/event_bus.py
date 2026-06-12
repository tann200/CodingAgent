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
import importlib as _importlib
import inspect
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple, Type as TypingType, TypeVar, Awaitable

from dataclasses import dataclass, field
from enum import IntEnum

if TYPE_CHECKING:
    from src.core.messaging.bus import MessageBus

from src.core.messaging.event_types import Event

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
                return asyncio.run(rv)  # type: ignore[return-value, arg-type]
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


# ── Typed event mapping ──────────────────────────────────────────────────
# Lazy per-event-type import strategy.
#
# Instead of importing all 90+ event classes in one monolithic block (which
# would lose the entire mapping if any single class fails to import), we store
# dotted module paths and import each class lazily on first use in
# _build_typed_event.  A failed import for "tool.execute.start" does not
# break "session.created".
# -------------------------------------------------------------------------

_EVENT_MAP_LOCK = threading.Lock()
_INHERITED_EVENT_FIELDS = frozenset({"_correlation_id", "correlation_id"})

# Dotted import paths for each event type.  Key: event string name.
# Value: (module_path, class_name, Optional[field_mapper])
_EVENT_IMPORT_PATHS: Dict[str, Tuple[str, str, Optional[Dict[str, str]]]] = {
    "agent.start": ("src.core.messaging.event_types", "AgentStart", None),
    "agent.end": ("src.core.messaging.event_types", "AgentEnd", None),
    "agent.status": ("src.core.messaging.event_types", "AgentStatus", None),
    "agent.mode_changed": ("src.core.messaging.event_types", "AgentModeChanged", None),
    "agent.plan_committed": ("src.core.messaging.event_types", "AgentPlanCommitted", None),
    "agent.message": ("src.core.messaging.event_types", "AgentMessage", None),
    "agent.waiting_for_user": ("src.core.messaging.event_types", "AgentWaitingForUser", None),
    "tool.execute.start": ("src.core.messaging.event_types", "ToolExecuteStart", None),
    "tool.invoked": ("src.core.messaging.event_types", "ToolInvoked", {"sessionUpdate": "session_update", "toolCallId": "tool_call_id"}),
    "tool.execute.finish": ("src.core.messaging.event_types", "ToolExecuteFinish", {"sessionUpdate": "session_update", "toolCallId": "tool_call_id", "rawOutput": "raw_output"}),
    "tool.execute.error": ("src.core.messaging.event_types", "ToolExecuteError", {"sessionUpdate": "session_update", "toolCallId": "tool_call_id", "rawOutput": "raw_output"}),
    "tool.result": ("src.core.messaging.event_types", "ToolResult", None),
    "tool.permission_required": ("src.core.messaging.event_types", "ToolPermissionRequired", None),
    "tool.doom_loop_detected": ("src.core.messaging.event_types", "ToolDoomLoopDetected", None),
    "spawn.permission_required": ("src.core.messaging.event_types", "SpawnPermissionRequired", None),
    "bash.approval_granted": ("src.core.messaging.event_types", "BashApprovalGranted", None),
    "bash.approval_denied": ("src.core.messaging.event_types", "BashApprovalDenied", None),
    "preview.pending": ("src.core.messaging.event_types", "PreviewPending", None),
    "preview.confirmed": ("src.core.messaging.event_types", "PreviewConfirmed", None),
    "preview.rejected": ("src.core.messaging.event_types", "PreviewRejected", None),
    "plan.requested": ("src.core.messaging.event_types", "PlanRequested", None),
    "plan.progress": ("src.core.messaging.event_types", "PlanProgress", None),
    "step.start": ("src.core.messaging.event_types", "StepStart", None),
    "step.finish": ("src.core.messaging.event_types", "StepFinish", None),
    "session.created": ("src.core.messaging.event_types", "SessionCreated", None),
    "session.new": ("src.core.messaging.event_types", "SessionNew", None),
    "session.hydrated": ("src.core.messaging.event_types", "SessionHydrated", {"messageHistory": "message_history", "currentTask": "current_task", "workingDir": "working_dir"}),
    "session.title_generated": ("src.core.messaging.event_types", "SessionTitleGenerated", None),
    "session.files_changed": ("src.core.messaging.event_types", "SessionFilesChanged", None),
    "session.registered": ("src.core.messaging.event_types", "SessionRegistered", None),
    "session.unregistered": ("src.core.messaging.event_types", "SessionUnregistered", None),
    "session.health_alert": ("src.core.messaging.event_types", "SessionHealthAlert", None),
    "session.request_state": ("src.core.messaging.event_types", "SessionRequestState", None),
    "provider.status.changed": ("src.core.messaging.event_types", "ProviderStatusChanged", None),
    "provider.unavailable": ("src.core.messaging.event_types", "ProviderUnavailable", None),
    "provider.models.list": ("src.core.messaging.event_types", "ProviderModelsList", None),
    "provider.models.cached": ("src.core.messaging.event_types", "ProviderModelsCached", None),
    "provider.models.empty": ("src.core.messaging.event_types", "ProviderModelsEmpty", None),
    "provider.models.updated": ("src.core.messaging.event_types", "ProviderModelsUpdated", None),
    "provider.selection.changed": ("src.core.messaging.event_types", "ProviderSelectionChanged", None),
    "provider.context_window": ("src.core.messaging.event_types", "ProviderContextWindow", None),
    "provider.config.missing": ("src.core.messaging.event_types", "ProviderConfigMissing", None),
    "provider.model.missing": ("src.core.messaging.event_types", "ProviderModelMissing", None),
    "provider.limit": ("src.core.messaging.event_types", "ProviderLimit", None),
    "response.stream_chunk": ("src.core.messaging.event_types", "ResponseStreamChunk", None),
    "response.stream_end": ("src.core.messaging.event_types", "ResponseStreamEnd", None),
    "model.token": ("src.core.messaging.event_types", "ModelToken", None),
    "llm.token": ("src.core.messaging.event_types", "LLMToken", None),
    "model.response": ("src.core.messaging.event_types", "ModelResponse", None),
    "model.routing": ("src.core.messaging.event_types", "ModelRouting", None),
    "model.routing.complete": ("src.core.messaging.event_types", "ModelRoutingComplete", None),
    "context.overflow": ("src.core.messaging.event_types", "ContextOverflow", {"context_window": "budget"}),
    "context.compacted": ("src.core.messaging.event_types", "ContextCompacted", None),
    "context.auto_compacted": ("src.core.messaging.event_types", "ContextAutoCompacted", None),
    "context.compact.failed": ("src.core.messaging.event_types", "ContextCompactFailed", None),
    "context.degraded": ("src.core.messaging.event_types", "ContextDegraded", None),
    "message.truncation": ("src.core.messaging.event_types", "MessageTruncation", None),
    "message.compaction_applied": ("src.core.messaging.event_types", "MessageCompactionApplied", None),
    "token.budget": ("src.core.messaging.event_types", "TokenBudget", None),
    "token.budget.update": ("src.core.messaging.event_types", "TokenBudgetUpdate", None),
    "token.budget.warning": ("src.core.messaging.event_types", "TokenBudgetWarning", None),
    "usage.turn_summary": ("src.core.messaging.event_types", "UsageTurnSummary", None),
    "usage.budget_exceeded": ("src.core.messaging.event_types", "UsageBudgetExceeded", None),
    "usage.subagent_cost": ("src.core.messaging.event_types", "UsageSubagentCost", None),
    "file.modified": ("src.core.messaging.event_types", "FileModified", None),
    "file.deleted": ("src.core.messaging.event_types", "FileDeleted", None),
    "file.diff.preview": ("src.core.messaging.event_types", "FileDiffPreview", None),
    "delegation.start": ("src.core.messaging.event_types", "DelegationStart", None),
    "delegation.finish": ("src.core.messaging.event_types", "DelegationFinish", None),
    "delegation.complete": ("src.core.messaging.event_types", "DelegationComplete", None),
    "agent.scout.files_discovered": ("src.core.messaging.event_types", "AgentScoutFilesDiscovered", None),
    "agent.researcher.doc_summary": ("src.core.messaging.event_types", "AgentResearcherDocSummary", None),
    "agent.reviewer.bug_found": ("src.core.messaging.event_types", "AgentReviewerBugFound", None),
    "scheduler.distill_request": ("src.core.messaging.event_types", "SchedulerDistillRequest", None),
    "scheduler.distill_completed": ("src.core.messaging.event_types", "SchedulerDistillCompleted", None),
    "mcp.server.status": ("src.core.messaging.event_types", "McpServerStatus", None),
    "mcp.tools.list_changed": ("src.core.messaging.event_types", "McpToolsListChanged", None),
    "config.reloaded": ("src.core.messaging.event_types", "ConfigReloaded", None),
    "system.settings": ("src.core.messaging.event_types", "SystemSettings", None),
    "orchestrator.startup": ("src.core.messaging.event_types", "OrchestratorStartup", None),
    "orchestrator.models.check.started": ("src.core.messaging.event_types", "OrchestratorModelsCheckStarted", None),
    "orchestrator.models.check.completed": ("src.core.messaging.event_types", "OrchestratorModelsCheckCompleted", None),
    "orchestrator.models.check.failed": ("src.core.messaging.event_types", "OrchestratorModelsCheckFailed", None),
    "ui.notification": ("src.core.messaging.event_types", "UiNotification", None),
    "hook.message": ("src.core.messaging.event_types", "HookMessage", None),
    "log.new": ("src.core.messaging.event_types", "LogEntry", None),
    "git.branch": ("src.core.messaging.event_types", "GitBranch", None),
    "working_dir.unavailable": ("src.core.messaging.event_types", "WorkingDirUnavailable", None),
    "role.changed": ("src.core.messaging.event_types", "RoleTransition", None),
    "role.transition": ("src.core.messaging.event_types", "RoleTransition", None),
    "retry.attempt": ("src.core.messaging.event_types", "RetryAttempt", None),
    "retry.succeeded": ("src.core.messaging.event_types", "RetrySucceeded", None),
    "retry.failed": ("src.core.messaging.event_types", "RetryFailed", None),
    "task.queue.updated": ("src.core.messaging.event_types", "TaskQueueUpdated", None),
    "task.turn_limit": ("src.core.messaging.event_types", "TaskTurnLimit", None),
    "perception.corrective_prompt": ("src.core.messaging.event_types", "PerceptionCorrectivePrompt", None),
}

# Cache of lazily-imported event classes: event_name -> (cls, field_mapper)
_EVENT_CLASS_CACHE: Dict[str, Tuple[TypingType[Event], Optional[Dict[str, str]]]] = {}
# Reverse cache: cls -> event_name (populated on first use)
_EVENT_NAME_FROM_CLASS: Dict[TypingType[Event], str] = {}
_CACHE_LOCK = threading.Lock()


def _get_event_name_map():
    """Return the event name map, building it lazily.

    Unlike the previous implementation that eagerly imported all 90+ event
    types in a single function call (where a single ImportError would lose
    the entire mapping), this implementation imports each event class on
    first use via ``_build_typed_event``.

    The returned tuple matches the legacy API for backward compatibility:
    ``(_EVENT_NAME_TO_TYPED, _EVENT_NAME_FROM_CLASS)``.
    """
    # We no longer pre-build the full mapping.  The dict is built lazily
    # by _build_typed_event.  Return empty dicts as placeholders — callers
    # that iterate the map (e.g., publish_typed) will get partial results,
    # but the critical lookup path in _build_typed_event works correctly.
    return _EVENT_CLASS_CACHE, _EVENT_NAME_FROM_CLASS


def _import_event_class(event_name: str) -> Optional[Tuple[TypingType[Event], Optional[Dict[str, str]]]]:
    """Lazily import a single event class by *event_name*.

    Thread-safe.  Returns None when the import fails (logged at warning).
    A single failed import does not affect other event types.
    """
    entry = _EVENT_IMPORT_PATHS.get(event_name)
    if entry is None:
        return None

    mod_path, cls_name, mapper = entry
    try:
        mod = _importlib.import_module(mod_path)
        cls = getattr(mod, cls_name)
    except Exception as exc:
        _logger.warning(
            "event_bus: failed to import %s from %s for event %r: %s",
            cls_name, mod_path, event_name, exc,
        )
        return None

    return (cls, mapper)


def _build_typed_event(event_name: str, payload: Optional[Any]) -> Optional[Event]:
    # Fast path: check cache
    cached = _EVENT_CLASS_CACHE.get(event_name)
    if cached is not None:
        cls, mapper = cached
    else:
        result = _import_event_class(event_name)
        if result is None:
            return None
        cls, mapper = result
        with _CACHE_LOCK:
            _EVENT_CLASS_CACHE[event_name] = result
            _EVENT_NAME_FROM_CLASS[cls] = event_name

    if payload is None:
        payload = {}

    if not isinstance(payload, dict):
        return None

    if mapper:
        mapped = {}
        for k, v in payload.items():
            if k not in _INHERITED_EVENT_FIELDS:
                mapped[mapper.get(k, k)] = v
    else:
        mapped = {k: v for k, v in payload.items() if k not in _INHERITED_EVENT_FIELDS}

    try:
        return cls(**mapped)
    except Exception as exc:
        _logger.warning(
            "_build_typed_event: failed to build %s from payload: %s — fields=%s",
            cls.__name__, exc, list(mapped.keys()),
        )
        return None


# ── Shared MessageBus singleton ──────────────────────────────────────────

_typed_bus: Optional["MessageBus"] = None
_typed_bus_lock = threading.Lock()


def get_typed_bus() -> "MessageBus":
    from src.core.messaging.bus import MessageBus

    global _typed_bus
    if _typed_bus is None:
        with _typed_bus_lock:
            if _typed_bus is None:
                _typed_bus = MessageBus(max_queue_size=512, worker_threads=4)
    return _typed_bus


def reset_typed_bus() -> None:
    global _typed_bus
    bus = _typed_bus
    _typed_bus = None
    if bus is not None:
        try:
            bus.shutdown(timeout=2.0)
        except Exception:
            pass


class EventBus:
    def __init__(self, typed_bus: Optional[MessageBus] = None) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[Callable[[Any], None]]] = {}
        self._agent_subscribers: Dict[str, List[Callable[[AgentMessage], None]]] = {}
        self._agent_ids: Set[str] = set()
        self._typed = typed_bus

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

        When a ``typed_bus`` is configured, also emits the typed equivalent on
        the shared ``MessageBus`` (if an ``EVENT_NAME_TO_TYPED`` entry exists).
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

        # Also emit typed event on MessageBus
        if self._typed is not None:
            typed = _build_typed_event(event_name, payload)
            if typed is not None:
                try:
                    self._typed.publish(typed)
                except Exception as exc:
                    _logger.debug("EventBus: typed publish failed for %s: %s", event_name, exc)

    def publish_typed(self, event: Event) -> None:
        """Publish a typed event on the MessageBus AND deliver to old-bus subscribers.

        The typed event is emitted on the ``MessageBus`` first, then old string-based
        ``EventBus`` subscribers receive the dict equivalent (via ``event.to_dict()``).

        Note: this does NOT call ``self.publish()`` to avoid double emission on the
        typed bus (``publish()`` also emits typed events via ``_build_typed_event``).
        """
        if self._typed is not None:
            try:
                self._typed.publish(event)
            except Exception as exc:
                _logger.warning("EventBus: typed publish failed for %s: %s", type(event).__name__, exc)

        # Deliver to old-bus subscribers directly (skip publish() → no re-typed)
        _, name_from_class = _get_event_name_map()
        event_name = name_from_class.get(type(event))
        if event_name is not None:
            payload = event.to_dict()
            cid = _current_correlation_id.get()
            if cid is not None and "_correlation_id" not in payload:
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
            called: set = set()
            all_subs = []
            for agent_id in self._agent_ids:
                for cb in self._agent_subscribers.get(agent_id, []):
                    if id(cb) not in called:
                        called.add(id(cb))
                        all_subs.append(cb)
            for cb in self._agent_subscribers.get("*", []):
                if id(cb) not in called:
                    all_subs.append(cb)
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

    The returned ``EventBus`` references the shared ``MessageBus`` singleton
    (see ``get_typed_bus()``) so that every ``publish()`` call also emits the
    typed equivalent on the typed bus.
    """
    global _default_bus
    if _default_bus is None:
        with _bus_lock:
            if _default_bus is None:  # double-checked lock
                _default_bus = EventBus(typed_bus=get_typed_bus())
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
