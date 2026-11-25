"""
Typed Event classes for all system event types.

Events are grouped by domain:
  - Agent lifecycle      (AgentStart, AgentStatus, AgentEnd, AgentModeChanged,
                          AgentPlanCommitted, AgentMessage, AgentWaitingForUser)
  - Tool execution       (ToolExecuteStart, ToolInvoked, ToolExecuteFinish, ToolExecuteError,
                          ToolResult, ToolPermissionRequired, ToolDoomLoopDetected)
  - Permission / approval (SpawnPermissionRequired, BashApprovalGranted, BashApprovalDenied)
  - Preview / plan       (PreviewPending, PreviewConfirmed, PreviewRejected,
                          PlanRequested, PlanProgress)
  - Step (graph nodes)   (StepStart, StepFinish)
  - Session              (SessionCreated, SessionNew, SessionHydrated, SessionTitleGenerated,
                          SessionFilesChanged, SessionRegistered, SessionUnregistered,
                          SessionHealthAlert, SessionRequestState)
  - Provider / model     (ProviderStatusChanged, ProviderModelsList, ProviderModelsCached,
                          ProviderModelsEmpty, ProviderModelsUpdated, ProviderSelectionChanged,
                          ProviderContextWindow, ProviderUnavailable, ProviderConfigMissing,
                          ProviderModelMissing, ProviderLimit)
  - Inference / streaming (ResponseStreamChunk, ResponseStreamEnd, ModelToken, LLMToken,
                           ModelResponse, ModelRouting, ModelRoutingComplete)
  - Context / memory     (ContextOverflow, ContextCompacted, ContextAutoCompacted,
                          ContextCompactFailed, ContextDegraded,
                          MessageTruncation, MessageCompactionApplied)
  - Token budget / usage (TokenBudget, TokenBudgetUpdate, TokenBudgetWarning,
                          UsageTurnSummary, UsageBudgetExceeded, UsageSubagentCost)
  - File system          (FileModified, FileDeleted, FileDiffPreview,
                          RepoChangeRecorded)
  - Role                 (RoleTransition)
  - Retry                (RetryAttempt, RetrySucceeded, RetryFailed)
  - Task                 (TaskQueueUpdated, TaskTurnLimit)
  - Delegation           (DelegationStart, DelegationFinish, DelegationComplete,
                          AgentScoutFilesDiscovered, AgentResearcherDocSummary,
                          AgentReviewerBugFound)
  - Scheduler            (SchedulerDistillRequest, SchedulerDistillCompleted)
  - MCP                  (McpServerStatus, McpToolsListChanged)
  - Config               (ConfigReloaded, SystemSettings)
  - Orchestrator         (OrchestratorStartup, OrchestratorModelsCheckStarted,
                          OrchestratorModelsCheckCompleted, OrchestratorModelsCheckFailed)
  - UI / notifications   (UiNotification, HookMessage, LogEntry,
                          GitBranch, WorkingDirUnavailable)
  - Perception           (PerceptionCorrectivePrompt)
  - Subagent dispatch    (SubagentDispatch, SubagentResult)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from src.core.messaging.events import Event


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------

@dataclass
class AgentStart(Event):
    """Published when an agent task begins."""
    task_id: str
    session_id: str
    task: str  # truncated to 200 chars at publish site


@dataclass
class AgentStatus(Event):
    """Published when the frontier loop updates the agent's visible status."""
    status: Literal["working", "idle"]
    node: str
    task: Optional[str] = None   # present when status=="working"
    turns: Optional[int] = None  # present when status=="idle"
    tool_calls: Optional[int] = None  # present when status=="idle"


@dataclass
class AgentEnd(Event):
    """Published when an agent task finishes (success or failure)."""
    task_id: str
    session_id: str
    status: str            # "completed" | "failed" | "cancelled"
    result: Optional[str] = None   # truncated to 500 chars
    error: Optional[str] = None


@dataclass
class AgentModeChanged(Event):
    """Published when the orchestrator switches execution mode (e.g. plan→act)."""
    mode: str
    tool: str


@dataclass
class AgentPlanCommitted(Event):
    """Published when the agent commits a multi-step plan."""
    step_count: int
    tool: str


@dataclass
class AgentMessage(Event):
    """Published when the agent sends a human-readable status message."""
    message: str
    attachments: Optional[List[Any]] = None
    status: Optional[str] = None


@dataclass
class AgentWaitingForUser(Event):
    """Published when the agent pauses for user input (question / choice)."""
    question: str
    choices: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

@dataclass
class ToolExecuteStart(Event):
    """Published just before a tool is executed."""
    tool: str
    args: Dict[str, Any]
    tool_call_id: str


@dataclass
class ToolInvoked(Event):
    """Published after a tool has been dispatched (status = invoked)."""
    session_update: str
    tool_call_id: str
    title: str
    status: Literal["invoked"]
    workdir: str


@dataclass
class ToolExecuteFinish(Event):
    """Published when a tool completes successfully."""
    session_update: str
    tool_call_id: str
    title: str
    status: Literal["completed"]
    content: Any
    raw_output: Any
    workdir: str


@dataclass
class ToolExecuteError(Event):
    """Published when a tool fails during execution."""
    session_update: str
    tool_call_id: str
    title: str
    status: Literal["failed"]
    content: Any
    error: str
    workdir: str


@dataclass
class ToolResult(Event):
    """Published with the raw tool result after a frontier-loop tool call."""
    tool: str
    result: Any
    turn: int


@dataclass
class ToolPermissionRequired(Event):
    """Published when the permission gateway needs user approval for a tool."""
    tool_id: str
    tool: str
    args: Dict[str, Any]


@dataclass
class ToolPermissionGranted(Event):
    """Published when operator approves a tool permission request."""
    tool_id: str


@dataclass
class ToolPermissionDenied(Event):
    """Published when operator denies a tool permission request."""
    tool_id: str


@dataclass
class ToolDoomLoopDetected(Event):
    """Published when a tool doom-loop is detected."""
    tool: str
    fingerprint: str
    behavior: Literal["ask"]


# ---------------------------------------------------------------------------
# Permission / approval
# ---------------------------------------------------------------------------

@dataclass
class SpawnPermissionRequired(Event):
    """Published when spawning a sub-agent requires explicit user approval."""
    tool: str
    role: str
    task: str   # truncated to 200 chars
    tool_id: str


@dataclass
class BashApprovalRequired(Event):
    """Published when a Tier-3 bash command requires explicit user confirmation."""
    tool_id: str
    command: str = ""


@dataclass
class BashApprovalGranted(Event):
    """Published from the TUI when the user approves a bash command."""
    tool_id: str


@dataclass
class BashApprovalDenied(Event):
    """Published from the TUI when the user denies a bash command."""
    tool_id: str


# ---------------------------------------------------------------------------
# Preview / plan
# ---------------------------------------------------------------------------

@dataclass
class PreviewPending(Event):
    """Published when a plan preview is ready for user review."""
    preview_id: str


@dataclass
class PreviewConfirmed(Event):
    """Published when the user confirms a pending preview."""
    preview_id: str = ""
    path: Optional[str] = None


@dataclass
class PreviewRejected(Event):
    """Published when the user rejects a pending preview."""
    preview_id: str = ""
    path: Optional[str] = None


@dataclass
class PlanRequested(Event):
    """Published when the graph wait-for-user node needs the user to approve a plan."""
    plan: Any
    blocked_tool: Optional[str]
    session_id: str


@dataclass
class PlanProgress(Event):
    """Published for each plan step progress update."""
    plan_progress: Dict[str, Any]


# ---------------------------------------------------------------------------
# Step (graph node progress)
# ---------------------------------------------------------------------------

@dataclass
class StepStart(Event):
    """Published at the start of each graph execution step."""
    step: int
    total: int
    tool: str
    description: str
    session_id: str


@dataclass
class StepFinish(Event):
    """Published at the end of each graph execution step."""
    step: int
    total: int
    tool: str
    ok: bool
    elapsed_ms: Optional[float]
    tool_call_count: int
    session_id: str


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class SessionCreated(Event):
    """Published when a new session is created."""
    session_id: str
    metadata: Optional[Dict[str, Any]] = None
    task_id: Optional[str] = None


@dataclass
class SessionNew(Event):
    """Published from the settings controller when a new blank session is requested."""


@dataclass
class SessionHydrated(Event):
    """Published when an existing session's history is loaded from storage."""
    session_id: str
    message_history: List[Dict[str, Any]]
    current_task: Optional[str]
    working_dir: str


@dataclass
class SessionTitleGenerated(Event):
    """Published when the inference loop auto-generates a session title."""
    title: str


@dataclass
class SessionFilesChanged(Event):
    """Published when the workspace file watcher detects changes."""
    files: List[Dict[str, str]]  # [{path, absolute}, ...]
    workdir: str
    is_git_repo: bool


@dataclass
class SessionRegistered(Event):
    """Published when a session is registered with the event bus (mock / dev mode)."""
    session_id: str


@dataclass
class SessionUnregistered(Event):
    """Published when a session is unregistered from the event bus."""
    session_id: str


@dataclass
class SessionHealthAlert(Event):
    """Published when a session health check detects an issue."""
    session_id: str
    message: str
    level: Literal["info", "warning", "error"] = "warning"


@dataclass
class SessionRequestState(Event):
    """Published from the TUI bridge to request the backend to persist session state."""
    session_id: str


# ---------------------------------------------------------------------------
# Provider / model
# ---------------------------------------------------------------------------

@dataclass
class ProviderStatusChanged(Event):
    """Published when a provider's connectivity status changes."""
    provider: str
    status: str  # "connected" | "disconnected" | "unknown"


@dataclass
class ProviderModelsList(Event):
    """Published when a fresh model list is fetched from a provider."""
    provider: str
    models: List[str]


@dataclass
class ProviderModelsCached(Event):
    """Published when the model list is served from cache (no fresh fetch needed)."""
    provider: str
    models: Optional[List[str]] = None


@dataclass
class ProviderModelsEmpty(Event):
    """Published when a provider returns an empty model list."""
    provider: str


@dataclass
class ProviderModelsUpdated(Event):
    """Published from the settings controller with a refreshed model list."""
    provider: str
    models: List[str]


@dataclass
class ProviderSelectionChanged(Event):
    """Published when the user selects a different provider or model."""
    provider: str
    model: Optional[str]


@dataclass
class ProviderContextWindow(Event):
    """Published after discovering a provider's context-window size."""
    provider: str
    model: str
    context_window: int


@dataclass
class ProviderUnavailable(Event):
    """Published when the primary provider cannot be initialised at startup."""
    reason: str


@dataclass
class ProviderConfigMissing(Event):
    """Published when a required provider config file is absent."""
    path: str


@dataclass
class ProviderModelMissing(Event):
    """Published when the configured model is not in the provider's list."""
    provider: Optional[str]
    requested: Optional[str]
    available: List[str]


@dataclass
class ProviderLimit(Event):
    """Published when the provider returns a rate-limit or quota error."""
    error: str


# ---------------------------------------------------------------------------
# Inference / streaming
# ---------------------------------------------------------------------------

@dataclass
class ResponseStreamChunk(Event):
    """Published for each streaming response chunk from the LLM."""
    chunk: str
    is_reasoning: bool


@dataclass
class ResponseStreamEnd(Event):
    """Published when the LLM streaming response is complete."""
    full_text: str


@dataclass
class ModelToken(Event):
    """Published for each token emitted during streaming."""
    text: str
    partial: bool
    full: Optional[str] = None  # present when partial=False


@dataclass
class LLMToken(Event):
    """Published alongside ModelToken for subscribers that need is_reasoning flag."""
    text: str
    partial: bool
    is_reasoning: bool = False
    full: Optional[str] = None  # present when partial=False


@dataclass
class ModelResponse(Event):
    """Published after a full LLM response (telemetry summary)."""
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency: float
    ts: float
    extra: Optional[Dict[str, Any]] = None


@dataclass
class ModelRouting(Event):
    """Published when the orchestrator selects which model to use."""
    selected: str
    provider: str
    available_models: List[str]


@dataclass
class ModelRoutingComplete(Event):
    """Published after routing is confirmed (post-switch)."""
    model: str
    provider: Optional[str]
    switched_provider: Optional[str]


# ---------------------------------------------------------------------------
# Context / memory
# ---------------------------------------------------------------------------

@dataclass
class ContextOverflow(Event):
    """Published when the prompt exceeds the context window."""
    prompt_tokens: int
    budget: int
    reserved: int
    session_id: str
    source: str  # "api_error" | "pre_flight"


@dataclass
class ContextCompacted(Event):
    """Published after successful manual compaction."""
    message: str
    method: str
    tokens_before: int
    tokens_after: int


@dataclass
class ContextAutoCompacted(Event):
    """Published after automatic compaction triggered by overflow."""
    method: str
    tokens_before: int
    tokens_after: int
    new_message_count: int
    session_id: str


@dataclass
class ContextDegraded(Event):
    """Published when context quality degrades (e.g. repeated truncation)."""
    session_id: str
    reason: str
    tokens_lost: Optional[int] = None


@dataclass
class ContextCompactFailed(Event):
    """Published when compaction fails."""
    message: str


@dataclass
class MessageTruncation(Event):
    """Published when old messages are dropped to fit context."""
    dropped_count: int
    dropped_tokens: int
    tokens_after: int


@dataclass
class MessageCompactionApplied(Event):
    """Published when the scheduler applies message compaction."""
    source: str
    original_count: int
    new_count: int
    dropped_count: int
    original_tokens: int
    new_tokens: int
    tokens_reduced: int


# ---------------------------------------------------------------------------
# Token budget / usage
# ---------------------------------------------------------------------------

@dataclass
class TokenBudget(Event):
    """Published with a human-readable token budget summary."""
    used: int
    limit: int
    percent: float
    warning: bool


@dataclass
class TokenBudgetUpdate(Event):
    """Published with raw token counts for metrics."""
    used_tokens: int
    max_tokens: int
    usage_ratio: float
    session_id: str


@dataclass
class UsageTurnSummary(Event):
    """Published at the end of each LLM turn with cost and token breakdown."""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    session_cost_usd: float
    model: str
    task_id: str


@dataclass
class UsageBudgetExceeded(Event):
    """Published when session cost exceeds the configured budget ceiling."""
    session_cost_usd: float
    budget_ceiling_usd: float


@dataclass
class TokenBudgetWarning(Event):
    """Published when token usage approaches the configured limit."""
    used: int
    limit: int
    percent: float
    message: Optional[str] = None


@dataclass
class UsageSubagentCost(Event):
    """Published per sub-agent turn with its cost and role."""
    child_session_id: str
    role: str
    cost_usd: float


# ---------------------------------------------------------------------------
# File system
# ---------------------------------------------------------------------------

@dataclass
class FileModified(Event):
    """Published when a tool modifies a file."""
    path: str
    tool: str
    workdir: str


@dataclass
class FileDeleted(Event):
    """Published when a tool deletes a file."""
    path: str
    workdir: str


@dataclass
class FileDiffPreview(Event):
    """Published when a diff preview is ready for user review."""
    path: str
    diff: str
    is_new_file: bool = False


@dataclass
class RepoChangeRecorded(Event):
    """Published for every file a modifying tool actually changed (audit P3.2)."""
    path: str
    tool: str
    change: str
    reason: str
    workdir: str
    bytes_after: int = 0


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------

@dataclass
class DelegationStart(Event):
    """Published when a new sub-agent delegation begins."""
    child_session_id: str
    parent_session_id: str
    role: str
    task: str


@dataclass
class DelegationFinish(Event):
    """Published when a sub-agent delegation completes."""
    child_session_id: str
    role: str
    ok: bool
    cost_usd: float


@dataclass
class DelegationComplete(Event):
    """Published when all delegated sub-agents have finished."""
    count: int
    keys: List[str]
    session_id: str


@dataclass
class AgentScoutFilesDiscovered(Event):
    """Published by a scout sub-agent when it has discovered relevant files."""
    files: List[str]
    agent_id: str
    result: Any


@dataclass
class AgentResearcherDocSummary(Event):
    """Published by a researcher sub-agent with a doc summary."""
    summary: str   # truncated to 500 chars at publish site
    agent_id: str


@dataclass
class AgentReviewerBugFound(Event):
    """Published by a reviewer sub-agent with discovered bugs."""
    bugs: List[Any]
    agent_id: str
    result: Any


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

@dataclass
class SchedulerDistillRequest(Event):
    """Published when the scheduler requests memory distillation."""
    source: str
    time: float


@dataclass
class SchedulerDistillCompleted(Event):
    """Published when distillation has completed."""
    source: str


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

@dataclass
class McpServerStatus(Event):
    """Published when an MCP server starts, stops, or changes state."""
    running: bool
    count: int
    has_error: bool = False
    server_names: List[str] = field(default_factory=list)
    # manager.py passes a rich dict; normalise to flat fields where possible
    servers: Optional[Dict[str, Any]] = None


@dataclass
class McpToolsListChanged(Event):
    """Published when the list of tools from an MCP server changes."""
    server: str
    params: Any


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ConfigReloaded(Event):
    """Published when configuration files are reloaded at runtime."""
    changed_paths: List[str]


# ---------------------------------------------------------------------------
# Orchestrator lifecycle
# ---------------------------------------------------------------------------

@dataclass
class OrchestratorStartup(Event):
    """Published when the orchestrator finishes initialisation."""
    time: float
    working_dir: str


@dataclass
class OrchestratorModelsCheckStarted(Event):
    """Published when orchestrator starts probing provider models."""
    payload: Dict[str, Any]


@dataclass
class OrchestratorModelsCheckCompleted(Event):
    """Published when orchestrator finishes probing provider models (success)."""
    payload: Dict[str, Any]


@dataclass
class OrchestratorModelsCheckFailed(Event):
    """Published when orchestrator model probe fails."""
    payload: Dict[str, Any]


# ---------------------------------------------------------------------------
# Graph node boundary / state transition validation
# ---------------------------------------------------------------------------

@dataclass
class NodeResultValidationFailed(Event):
    """Published when a cognitive graph node returns a state update that violates"""
    node_name: str
    reason: str
    details: List[str]
    session_id: Optional[str]


# ---------------------------------------------------------------------------
# UI / notifications
# ---------------------------------------------------------------------------

@dataclass
class UiNotification(Event):
    """Published to surface a human-readable notification in the TUI."""
    level: Literal["info", "warning", "error"]
    message: str
    source: Optional[str] = None


@dataclass
class HookMessage(Event):
    """Published by shell hooks with a user-visible message."""
    tool_name: str
    event: str
    message: str


@dataclass
class GitBranch(Event):
    """Published with git status for the working directory."""
    branch: str
    dirty: bool
    ahead: int
    behind: int


@dataclass
class WorkingDirUnavailable(Event):
    """Published when the configured working directory is inaccessible."""
    path: str
    error: str


@dataclass
class SystemSettings(Event):
    """Published from the TUI bridge with resolved configuration values."""
    active_mode: str
    theme: str
    context_window: int
    default_provider: str
    default_model: str
    providers: List[Dict[str, Any]]
    autonomous_mode: bool
    max_turns: int


@dataclass
class LogEntry(Event):
    """Published for each log line emitted by the core logger."""
    level: str
    message: str
    logger: Optional[str] = None


@dataclass
class RoleTransition(Event):
    """Published when the agent's role changes (e.g. coding → planning)."""
    role: str


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

@dataclass
class RetryAttempt(Event):
    """Published when a retry is attempted after a failure."""
    attempt: int
    max_attempts: int
    reason: str


@dataclass
class RetrySucceeded(Event):
    """Published when a retry attempt succeeds."""
    attempt: int
    reason: str


@dataclass
class RetryFailed(Event):
    """Published when all retry attempts are exhausted."""
    attempt: int
    max_attempts: int
    reason: str


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@dataclass
class TaskQueueUpdated(Event):
    """Published when the pending task queue changes."""
    pending_tasks: int
    queue_size: int
    session_id: str


# ---------------------------------------------------------------------------
# Perception
# ---------------------------------------------------------------------------

@dataclass
class PerceptionCorrectivePrompt(Event):
    """Published when the perception node injects a corrective prompt."""
    session_id: str
    attempt: int
    reason: str
    model_tier: str
    truncated_yaml: str


@dataclass
class TaskTurnLimit(Event):
    """Published when the task hits its maximum turn count."""
    turn_count: int
    max_turns: int


# ---------------------------------------------------------------------------
# Subagent dispatch
# ---------------------------------------------------------------------------

@dataclass
class SubagentDispatch(Event):
    """Published when a sub-agent is dispatched via the cross-session bus."""
    delegation_key: str
    role: str
    task: str


@dataclass
class SubagentResult(Event):
    """Published when a sub-agent returns its result via the cross-session bus."""
    delegation_key: str
    role: str
    task: str
    result: Any
