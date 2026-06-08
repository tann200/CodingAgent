"""
Message Bus - Typed Event System

This package provides a typed, reliable event delivery system to replace
the untyped EventBus. Key features:

- Type-safe event delivery
- Error isolation (failed handlers don't kill publishers)
- Delivery guarantees with explicit drop logging
- Metrics for observability
- Correlation IDs for distributed tracing
"""

from src.core.messaging.events import Event
from src.core.messaging.bus import MessageBus, EventHandler
from src.core.messaging.metrics import MessageBusMetrics
from src.core.messaging.event_types import (
    # Agent lifecycle
    AgentStart, AgentStatus, AgentEnd, AgentModeChanged, AgentPlanCommitted,
    AgentMessage, AgentWaitingForUser,
    # Tool execution
    ToolExecuteStart, ToolInvoked, ToolExecuteFinish, ToolExecuteError,
    ToolResult, ToolPermissionRequired, ToolDoomLoopDetected,
    # Permission / approval
    SpawnPermissionRequired, BashApprovalGranted, BashApprovalDenied,
    # Preview / plan
    PreviewPending, PreviewConfirmed, PreviewRejected, PlanRequested, PlanProgress,
    # Step
    StepStart, StepFinish,
    # Session
    SessionCreated, SessionNew, SessionHydrated, SessionTitleGenerated,
    SessionFilesChanged, SessionRegistered, SessionUnregistered,
    SessionHealthAlert, SessionRequestState,
    # Provider / model
    ProviderStatusChanged, ProviderModelsList, ProviderModelsCached, ProviderModelsEmpty,
    ProviderModelsUpdated, ProviderSelectionChanged, ProviderContextWindow,
    ProviderUnavailable, ProviderConfigMissing, ProviderModelMissing, ProviderLimit,
    # Inference / streaming
    ResponseStreamChunk, ResponseStreamEnd, ModelToken, LLMToken,
    ModelResponse, ModelRouting, ModelRoutingComplete,
    # Context / memory
    ContextOverflow, ContextCompacted, ContextAutoCompacted, ContextDegraded,
    ContextCompactFailed, MessageTruncation, MessageCompactionApplied,
    # Token budget / usage
    TokenBudget, TokenBudgetUpdate, TokenBudgetWarning,
    UsageTurnSummary, UsageBudgetExceeded, UsageSubagentCost,
    # File system
    FileModified, FileDeleted, FileDiffPreview,
    # Delegation
    DelegationStart, DelegationFinish, DelegationComplete,
    AgentScoutFilesDiscovered, AgentResearcherDocSummary, AgentReviewerBugFound,
    # Scheduler
    SchedulerDistillRequest, SchedulerDistillCompleted,
    # MCP
    McpServerStatus, McpToolsListChanged,
    # Config
    ConfigReloaded, SystemSettings,
    # Orchestrator lifecycle
    OrchestratorStartup, OrchestratorModelsCheckStarted,
    OrchestratorModelsCheckCompleted, OrchestratorModelsCheckFailed,
    # UI / notifications
    UiNotification, HookMessage, LogEntry, GitBranch, WorkingDirUnavailable,
    # Role
    RoleTransition,
    # Retry
    RetryAttempt, RetrySucceeded, RetryFailed,
    # Task
    TaskQueueUpdated, TaskTurnLimit,
    # Perception
    PerceptionCorrectivePrompt,
    # Subagent dispatch
    SubagentDispatch, SubagentResult,
)

__all__ = [
    "Event",
    "MessageBus",
    "EventHandler",
    "MessageBusMetrics",
    # Agent lifecycle
    "AgentStart", "AgentStatus", "AgentEnd", "AgentModeChanged", "AgentPlanCommitted",
    "AgentMessage", "AgentWaitingForUser",
    # Tool execution
    "ToolExecuteStart", "ToolInvoked", "ToolExecuteFinish", "ToolExecuteError",
    "ToolResult", "ToolPermissionRequired", "ToolDoomLoopDetected",
    # Permission / approval
    "SpawnPermissionRequired", "BashApprovalGranted", "BashApprovalDenied",
    # Preview / plan
    "PreviewPending", "PreviewConfirmed", "PreviewRejected", "PlanRequested", "PlanProgress",
    # Step
    "StepStart", "StepFinish",
    # Session
    "SessionCreated", "SessionNew", "SessionHydrated", "SessionTitleGenerated",
    "SessionFilesChanged", "SessionRegistered", "SessionUnregistered",
    "SessionHealthAlert", "SessionRequestState",
    # Provider / model
    "ProviderStatusChanged", "ProviderModelsList", "ProviderModelsCached", "ProviderModelsEmpty",
    "ProviderModelsUpdated", "ProviderSelectionChanged", "ProviderContextWindow",
    "ProviderUnavailable", "ProviderConfigMissing", "ProviderModelMissing", "ProviderLimit",
    # Inference / streaming
    "ResponseStreamChunk", "ResponseStreamEnd", "ModelToken", "LLMToken",
    "ModelResponse", "ModelRouting", "ModelRoutingComplete",
    # Context / memory
    "ContextOverflow", "ContextCompacted", "ContextAutoCompacted", "ContextDegraded",
    "ContextCompactFailed", "MessageTruncation", "MessageCompactionApplied",
    # Token budget / usage
    "TokenBudget", "TokenBudgetUpdate", "TokenBudgetWarning",
    "UsageTurnSummary", "UsageBudgetExceeded", "UsageSubagentCost",
    # File system
    "FileModified", "FileDeleted", "FileDiffPreview",
    # Delegation
    "DelegationStart", "DelegationFinish", "DelegationComplete",
    "AgentScoutFilesDiscovered", "AgentResearcherDocSummary", "AgentReviewerBugFound",
    # Scheduler
    "SchedulerDistillRequest", "SchedulerDistillCompleted",
    # MCP
    "McpServerStatus", "McpToolsListChanged",
    # Config
    "ConfigReloaded", "SystemSettings",
    # Orchestrator lifecycle
    "OrchestratorStartup", "OrchestratorModelsCheckStarted",
    "OrchestratorModelsCheckCompleted", "OrchestratorModelsCheckFailed",
    # UI / notifications
    "UiNotification", "HookMessage", "LogEntry", "GitBranch", "WorkingDirUnavailable",
    # Role
    "RoleTransition",
    # Retry
    "RetryAttempt", "RetrySucceeded", "RetryFailed",
    # Task
    "TaskQueueUpdated", "TaskTurnLimit",
    # Perception
    "PerceptionCorrectivePrompt",
    # Subagent dispatch
    "SubagentDispatch", "SubagentResult",
]
