"""
DualPublishBus — wraps the old EventBus to also emit typed events on MessageBus.

Migration strategy:
  Every ``EventBus.publish(event_name, payload)`` call is intercepted.
  The adapter looks up *event_name* in a static table, constructs the
  corresponding typed ``Event`` subclass (applying camelCase→snake_case field
  mapping where needed), and publishes on the ``MessageBus`` in addition to
  the old bus.

  This means **zero** publish sites need to change during the migration.
  Once all sites are dual-emitting, the old EventBus can be removed and
  all callers can publish directly to the ``MessageBus``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Tuple, Type

from src.core.messaging import Event, MessageBus
from src.core.messaging.event_types import (
    AgentEnd,
    AgentMessage,
    AgentModeChanged,
    AgentPlanCommitted,
    AgentResearcherDocSummary,
    AgentReviewerBugFound,
    AgentScoutFilesDiscovered,
    AgentStart,
    AgentStatus,
    AgentWaitingForUser,
    BashApprovalDenied,
    BashApprovalGranted,
    ConfigReloaded,
    ContextAutoCompacted,
    ContextCompactFailed,
    ContextCompacted,
    ContextDegraded,
    ContextOverflow,
    DelegationComplete,
    DelegationFinish,
    DelegationStart,
    FileDeleted,
    FileDiffPreview,
    FileModified,
    GitBranch,
    HookMessage,
    LLMToken,
    LogEntry,
    McpServerStatus,
    McpToolsListChanged,
    MessageCompactionApplied,
    MessageTruncation,
    ModelResponse,
    ModelRouting,
    ModelRoutingComplete,
    ModelToken,
    OrchestratorModelsCheckCompleted,
    OrchestratorModelsCheckFailed,
    OrchestratorModelsCheckStarted,
    OrchestratorStartup,
    PerceptionCorrectivePrompt,
    PlanProgress,
    PlanRequested,
    PreviewConfirmed,
    PreviewPending,
    PreviewRejected,
    ProviderConfigMissing,
    ProviderContextWindow,
    ProviderLimit,
    ProviderModelMissing,
    ProviderModelsCached,
    ProviderModelsEmpty,
    ProviderModelsList,
    ProviderModelsUpdated,
    ProviderSelectionChanged,
    ProviderStatusChanged,
    ProviderUnavailable,
    ResponseStreamChunk,
    ResponseStreamEnd,
    RetryAttempt,
    RetryFailed,
    RetrySucceeded,
    RoleTransition,
    SchedulerDistillCompleted,
    SchedulerDistillRequest,
    SessionCreated,
    SessionFilesChanged,
    SessionHealthAlert,
    SessionHydrated,
    SessionNew,
    SessionRegistered,
    SessionRequestState,
    SessionTitleGenerated,
    SessionUnregistered,
    SpawnPermissionRequired,
    StepFinish,
    StepStart,
    SystemSettings,
    TaskQueueUpdated,
    TaskTurnLimit,
    TokenBudget,
    TokenBudgetUpdate,
    TokenBudgetWarning,
    ToolDoomLoopDetected,
    ToolExecuteError,
    ToolExecuteFinish,
    ToolExecuteStart,
    ToolInvoked,
    ToolPermissionRequired,
    ToolResult,
    UiNotification,
    UsageBudgetExceeded,
    UsageSubagentCost,
    UsageTurnSummary,
    WorkingDirUnavailable,
)

logger = logging.getLogger(__name__)

# ── Event name → typed class mapping ──────────────────────────────────────
# Each entry maps an old EventBus event-name string to a tuple of:
#   (typed_event_class, field_mapper_or_None)
#
# *field_mapper* is a dict of ``{camelCase_key: snake_case_field}`` for
# payload keys that don't match the typed event's constructor parameter names.
# When ``None``, the payload keys are assumed to already match (after removing
# ``_correlation_id`` which is handled by the Event base class).
#
# Tip: grep ``\.publish\(`` in ``src/`` and ``tui/`` to find new publish
# sites that need an entry here.
# ---------------------------------------------------------------------------

EVENT_NAME_TO_TYPED: Dict[str, Tuple[Type[Event], Optional[Dict[str, str]]]] = {
    # Agent lifecycle
    "agent.start": (AgentStart, None),
    "agent.end": (AgentEnd, None),
    "agent.status": (AgentStatus, None),
    "agent.mode_changed": (AgentModeChanged, None),
    "agent.plan_committed": (AgentPlanCommitted, None),
    "agent.message": (AgentMessage, None),
    "agent.waiting_for_user": (AgentWaitingForUser, None),
    # Tool execution
    "tool.execute.start": (ToolExecuteStart, None),
    "tool.invoked": (ToolInvoked, {
        "sessionUpdate": "session_update",
        "toolCallId": "tool_call_id",
    }),
    "tool.execute.finish": (ToolExecuteFinish, {
        "sessionUpdate": "session_update",
        "toolCallId": "tool_call_id",
        "rawOutput": "raw_output",
    }),
    "tool.execute.error": (ToolExecuteError, {
        "sessionUpdate": "session_update",
        "toolCallId": "tool_call_id",
        "rawOutput": "raw_output",
    }),
    "tool.result": (ToolResult, None),
    "tool.permission_required": (ToolPermissionRequired, None),
    "tool.doom_loop_detected": (ToolDoomLoopDetected, None),
    # Permission / approval
    "spawn.permission_required": (SpawnPermissionRequired, None),
    "bash.approval_granted": (BashApprovalGranted, None),
    "bash.approval_denied": (BashApprovalDenied, None),
    # Preview / plan
    "preview.pending": (PreviewPending, None),
    "preview.confirmed": (PreviewConfirmed, None),
    "preview.rejected": (PreviewRejected, None),
    "plan.requested": (PlanRequested, None),
    "plan.progress": (PlanProgress, None),
    # Step
    "step.start": (StepStart, None),
    "step.finish": (StepFinish, None),
    # Session
    "session.created": (SessionCreated, None),
    "session.new": (SessionNew, None),
    "session.hydrated": (SessionHydrated, {
        "messageHistory": "message_history",
        "currentTask": "current_task",
        "workingDir": "working_dir",
    }),
    "session.title_generated": (SessionTitleGenerated, None),
    "session.files_changed": (SessionFilesChanged, None),
    "session.registered": (SessionRegistered, None),
    "session.unregistered": (SessionUnregistered, None),
    "session.health_alert": (SessionHealthAlert, None),
    "session.request_state": (SessionRequestState, None),
    # Provider / model
    "provider.status.changed": (ProviderStatusChanged, None),
    "provider.unavailable": (ProviderUnavailable, None),
    "provider.models.list": (ProviderModelsList, None),
    "provider.models.cached": (ProviderModelsCached, None),
    "provider.models.empty": (ProviderModelsEmpty, None),
    "provider.models.updated": (ProviderModelsUpdated, None),
    "provider.selection.changed": (ProviderSelectionChanged, None),
    "provider.context_window": (ProviderContextWindow, None),
    "provider.config.missing": (ProviderConfigMissing, None),
    "provider.model.missing": (ProviderModelMissing, None),
    "provider.limit": (ProviderLimit, None),
    # Inference / streaming
    "response.stream_chunk": (ResponseStreamChunk, None),
    "response.stream_end": (ResponseStreamEnd, None),
    "model.token": (ModelToken, None),
    "llm.token": (LLMToken, None),
    "model.response": (ModelResponse, None),
    "model.routing": (ModelRouting, None),
    "model.routing.complete": (ModelRoutingComplete, None),
    # Context / memory
    "context.overflow": (ContextOverflow, {
        "context_window": "budget",  # one publish site uses context_window key
    }),
    "context.compacted": (ContextCompacted, None),
    "context.auto_compacted": (ContextAutoCompacted, None),
    "context.compact.failed": (ContextCompactFailed, None),
    "context.degraded": (ContextDegraded, None),
    "message.truncation": (MessageTruncation, None),
    "message.compaction_applied": (MessageCompactionApplied, None),
    # Token budget / usage
    "token.budget": (TokenBudget, None),
    "token.budget.update": (TokenBudgetUpdate, None),
    "token.budget.warning": (TokenBudgetWarning, None),
    "usage.turn_summary": (UsageTurnSummary, None),
    "usage.budget_exceeded": (UsageBudgetExceeded, None),
    "usage.subagent_cost": (UsageSubagentCost, None),
    # File system
    "file.modified": (FileModified, None),
    "file.deleted": (FileDeleted, None),
    "file.diff.preview": (FileDiffPreview, None),
    # Delegation
    "delegation.start": (DelegationStart, None),
    "delegation.finish": (DelegationFinish, None),
    "delegation.complete": (DelegationComplete, None),
    "agent.scout.files_discovered": (AgentScoutFilesDiscovered, None),
    "agent.researcher.doc_summary": (AgentResearcherDocSummary, None),
    "agent.reviewer.bug_found": (AgentReviewerBugFound, None),
    # Scheduler
    "scheduler.distill_request": (SchedulerDistillRequest, None),
    "scheduler.distill_completed": (SchedulerDistillCompleted, None),
    # MCP
    "mcp.server.status": (McpServerStatus, None),
    "mcp.tools.list_changed": (McpToolsListChanged, None),
    # Config
    "config.reloaded": (ConfigReloaded, None),
    "system.settings": (SystemSettings, None),
    # Orchestrator
    "orchestrator.startup": (OrchestratorStartup, None),
    "orchestrator.models.check.started": (OrchestratorModelsCheckStarted, None),
    "orchestrator.models.check.completed": (OrchestratorModelsCheckCompleted, None),
    "orchestrator.models.check.failed": (OrchestratorModelsCheckFailed, None),
    # UI / notifications
    "ui.notification": (UiNotification, None),
    "hook.message": (HookMessage, None),
    "log.new": (LogEntry, None),
    "git.branch": (GitBranch, None),
    "working_dir.unavailable": (WorkingDirUnavailable, None),
    # Role
    "role.changed": (RoleTransition, None),
    "role.transition": (RoleTransition, None),
    # Retry
    "retry.attempt": (RetryAttempt, None),
    "retry.succeeded": (RetrySucceeded, None),
    "retry.failed": (RetryFailed, None),
    # Task
    "task.queue.updated": (TaskQueueUpdated, None),
    "task.turn_limit": (TaskTurnLimit, None),
    # Perception
    "perception.corrective_prompt": (PerceptionCorrectivePrompt, None),
}

# Derived: set of all registered event names (used by publish_typed for
# backward mapping — typed event → event name string).
_EVENT_NAME_FROM_CLASS: Dict[Type[Event], str] = {
    cls: name for name, (cls, _) in EVENT_NAME_TO_TYPED.items()
}


# ── Shared MessageBus singleton ──────────────────────────────────────────

_typed_bus: Optional[MessageBus] = None
_bus_lock = threading.Lock()


def get_typed_bus() -> MessageBus:
    """Return the process-wide MessageBus singleton."""
    global _typed_bus
    if _typed_bus is None:
        with _bus_lock:
            if _typed_bus is None:
                _typed_bus = MessageBus(max_queue_size=512, worker_threads=4)
    return _typed_bus


def reset_typed_bus() -> None:
    """Reset the singleton (for testing)."""
    global _typed_bus
    bus = _typed_bus
    _typed_bus = None
    if bus is not None:
        try:
            bus.shutdown(timeout=2.0)
        except Exception:
            pass


# ── Dual-publish adapter ─────────────────────────────────────────────────

# Fields on Event base class that are injected automatically and should NOT
# be passed to the typed event constructor from the old bus payload.
_INHERITED_EVENT_FIELDS = frozenset({"_correlation_id", "correlation_id"})


def _build_typed_event(event_name: str, payload: Optional[Any]) -> Optional[Event]:
    """Build a typed ``Event`` from an old-bus *event_name* and *payload*.

    Returns ``None`` when the event name has no registered typed class or
    when the payload cannot be converted (the old bus still delivers in
    those cases).
    """
    entry = EVENT_NAME_TO_TYPED.get(event_name)
    if entry is None:
        return None
    cls, mapper = entry

    if payload is None:
        payload = {}

    if not isinstance(payload, dict):
        # Non-dict payloads (rare: e.g. a plain string or int) can't be
        # mapped to typed event fields — skip typed emission.
        return None

    # Map camelCase keys → snake_case fields
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
        logger.debug(
            "DualPublishBus: failed to build %s from payload: %s",
            cls.__name__,
            exc,
        )
        return None


class DualPublishBus:
    """Wraps an old ``EventBus`` to dual-publish on both buses.

    Every call to ``publish()`` also emits the equivalent typed event on the
    shared ``MessageBus`` singleton (see ``get_typed_bus()``).

    All other methods (``subscribe``, ``unsubscribe``, agent-specific methods,
    etc.) are delegated transparently to the wrapped old bus.
    """

    def __init__(self, old_bus: Any, typed_bus: Optional[MessageBus] = None) -> None:
        self._old = old_bus
        self._typed = typed_bus or get_typed_bus()

    # ── publish — the only method we intercept ──────────────────────────

    def publish(
        self,
        event_name: str,
        payload: Any = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        # 1. Always deliver via the old bus (existing subscribers unaffected).
        self._old.publish(event_name, payload, correlation_id)

        # 2. Also emit a typed event on the MessageBus.
        typed = _build_typed_event(event_name, payload)
        if typed is not None:
            try:
                self._typed.publish(typed)
            except Exception as exc:
                logger.debug(
                    "DualPublishBus: typed publish failed for %s: %s",
                    event_name,
                    exc,
                )

    # ── Helpers for direct typed publishing ────────────────────────────

    def publish_typed(self, event: Event) -> None:
        """Publish a typed event on both buses.

        This is the forward direction: new code can use typed events directly,
        and the old bus still receives the event (as a dict) for legacy
        subscribers.
        """
        try:
            self._typed.publish(event)
        except Exception as exc:
            logger.debug(
                "DualPublishBus: typed publish failed for %s: %s",
                type(event).__name__,
                exc,
            )
        # Also emit on the old bus for backward compat
        event_name = _EVENT_NAME_FROM_CLASS.get(type(event))
        if event_name is not None:
            self._old.publish(event_name, event.to_dict())

    # ── Everything else delegates to the old bus ────────────────────────

    def subscribe(self, event_name: str, callback: Any) -> None:
        self._old.subscribe(event_name, callback)

    def unsubscribe(self, event_name: str, callback: Any) -> None:
        self._old.unsubscribe(event_name, callback)

    def has_subscribers(self, event_name: str) -> bool:
        return self._old.has_subscribers(event_name)

    def subscribe_to_agent(self, agent_id: str, callback: Any) -> None:
        self._old.subscribe_to_agent(agent_id, callback)

    def unsubscribe_from_agent(self, agent_id: str, callback: Any) -> None:
        self._old.unsubscribe_from_agent(agent_id, callback)

    def publish_to_agent(
        self,
        agent_id: str,
        payload: Any,
        priority: Any = None,
        reply_to: Optional[str] = None,
    ) -> None:
        self._old.publish_to_agent(agent_id, payload, priority, reply_to)

    def broadcast_to_agents(self, payload: Any, priority: Any = None) -> None:
        self._old.broadcast_to_agents(payload, priority)

    def publish_dispatch(self, event: Any) -> None:
        self._old.publish_dispatch(event)

    def publish_dispatch_result(self, event: Any) -> None:
        self._old.publish_dispatch_result(event)

    def subscribe_dispatch(self, callback: Any) -> None:
        self._old.subscribe_dispatch(callback)

    def subscribe_dispatch_result(self, callback: Any) -> None:
        self._old.subscribe_dispatch_result(callback)

    def list_registered_agents(self) -> Any:
        return self._old.list_registered_agents()

    def publish_with_identity(
        self,
        event_name: str,
        payload: Any,
        sender_id: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> None:
        self._old.publish_with_identity(event_name, payload, sender_id, priority)

    def subscribe_to_topic(self, topic: str, callback: Any) -> None:
        self._old.subscribe_to_topic(topic, callback)

    def publish_to_topic(
        self,
        topic: str,
        payload: Any,
        sender_id: Optional[str] = None,
    ) -> None:
        self._old.publish_to_topic(topic, payload, sender_id)

    def subscribe_to_preview_complete(self, callback: Any) -> None:
        self._old.subscribe_to_preview_complete(callback)

    def __getattr__(self, name: str) -> Any:
        """Catch-all: delegate any unknown attribute to the old bus."""
        return getattr(self._old, name)
