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
  - File system          (FileModified, FileDeleted, FileDiffPreview)
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
    """
    Published when an agent task begins.

    Publishers:
        src/server/task_endpoints.py:154

    Subscribers:
        TUI bridge (agent status widget)

    Use-case: Start a spinner / "working" indicator in the UI.
    """
    task_id: str
    session_id: str
    task: str  # truncated to 200 chars at publish site


@dataclass
class AgentStatus(Event):
    """
    Published when the frontier loop updates the agent's visible status.

    Publishers:
        src/core/orchestration/graph/nodes/frontier_loop_node.py:837,1005

    Use-case: UI polls this to show "working…" or "idle" in the header bar.
    """
    status: Literal["working", "idle"]
    node: str
    task: Optional[str] = None   # present when status=="working"
    turns: Optional[int] = None  # present when status=="idle"
    tool_calls: Optional[int] = None  # present when status=="idle"


@dataclass
class AgentEnd(Event):
    """
    Published when an agent task finishes (success or failure).

    Publishers:
        src/server/task_endpoints.py:211

    Use-case: Stop spinner, show result summary or error banner.
    """
    task_id: str
    session_id: str
    status: str            # "completed" | "failed" | "cancelled"
    result: Optional[str] = None   # truncated to 500 chars
    error: Optional[str] = None


@dataclass
class AgentModeChanged(Event):
    """
    Published when the orchestrator switches execution mode (e.g. plan→act).

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:749

    Use-case: UI can display current mode; analytics can track mode switches.
    """
    mode: str
    tool: str


@dataclass
class AgentPlanCommitted(Event):
    """
    Published when the agent commits a multi-step plan.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:759

    Use-case: Show plan step count in UI progress bar.
    """
    step_count: int
    tool: str


@dataclass
class AgentMessage(Event):
    """
    Published when the agent sends a human-readable status message.

    Publishers:
        src/tools/interaction_tools.py:229

    Subscribers:
        TUI bridge (agent message widget)

    Use-case: Display agent-generated message in chat log.
    """
    message: str
    attachments: Optional[List[Any]] = None
    status: Optional[str] = None


@dataclass
class AgentWaitingForUser(Event):
    """
    Published when the agent pauses for user input (question / choice).

    Publishers:
        src/tools/interaction_tools.py:66

    Use-case: Show question prompt with selectable options.
    """
    question: str
    choices: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

@dataclass
class ToolExecuteStart(Event):
    """
    Published just before a tool is executed.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:1034

    Use-case: Create a "running" tool widget in the TUI chat view.
    """
    tool: str
    args: Dict[str, Any]
    tool_call_id: str


@dataclass
class ToolInvoked(Event):
    """
    Published after a tool has been dispatched (status = invoked).

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:804

    Use-case: Persist invocation record in session store.
    """
    session_update: str
    tool_call_id: str
    title: str
    status: Literal["invoked"]
    workdir: str


@dataclass
class ToolExecuteFinish(Event):
    """
    Published when a tool completes successfully.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:880

    Use-case: Update tool widget to show result; write session record.
    """
    session_update: str
    tool_call_id: str
    title: str
    status: Literal["completed"]
    content: Any
    raw_output: Any
    workdir: str


@dataclass
class ToolExecuteError(Event):
    """
    Published when a tool fails during execution.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:673

    Use-case: Mark tool widget as failed; log error for diagnostics.
    """
    session_update: str
    tool_call_id: str
    title: str
    status: Literal["failed"]
    content: Any
    error: str
    workdir: str


@dataclass
class ToolResult(Event):
    """
    Published with the raw tool result after a frontier-loop tool call.

    Publishers:
        src/core/orchestration/graph/nodes/frontier_loop_node.py:710

    Use-case: Audit log; metrics pipeline.
    """
    tool: str
    result: Any
    turn: int


@dataclass
class ToolPermissionRequired(Event):
    """
    Published when the permission gateway needs user approval for a tool.

    Publishers:
        src/core/orchestration/permission_gateway.py:701
        src/core/orchestration/tool_execution_service.py:334

    Use-case: Show approval modal in TUI; block execution until granted/denied.
    """
    tool_id: str
    tool: str
    args: Dict[str, Any]


@dataclass
class ToolDoomLoopDetected(Event):
    """
    Published when a tool doom-loop is detected.

    Publishers:
        src/core/orchestration/loop_guards.py:401

    Use-case: Surface warning to user; interrupt the loop.
    """
    tool: str
    fingerprint: str
    behavior: Literal["ask"]


# ---------------------------------------------------------------------------
# Permission / approval
# ---------------------------------------------------------------------------

@dataclass
class SpawnPermissionRequired(Event):
    """
    Published when spawning a sub-agent requires explicit user approval.

    Publishers:
        src/core/orchestration/permission_gateway.py:685

    Use-case: Show spawn-approval modal; block delegation until resolved.
    """
    tool: str
    role: str
    task: str   # truncated to 200 chars
    tool_id: str


@dataclass
class BashApprovalGranted(Event):
    """
    Published from the TUI when the user approves a bash command.

    Publishers:
        tui/src/ui/_bridge_tools.py:272

    Use-case: Resume blocked bash tool execution.
    """
    tool_id: str


@dataclass
class BashApprovalDenied(Event):
    """
    Published from the TUI when the user denies a bash command.

    Publishers:
        tui/src/ui/_bridge_tools.py:278

    Use-case: Cancel blocked bash tool; show denial in tool widget.
    """
    tool_id: str


# ---------------------------------------------------------------------------
# Preview / plan
# ---------------------------------------------------------------------------

@dataclass
class PreviewPending(Event):
    """
    Published when a plan preview is ready for user review.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:928

    Use-case: Show preview / diff panel in TUI.
    """
    preview_id: str


@dataclass
class PreviewConfirmed(Event):
    """
    Published when the user confirms a pending preview.

    Publishers:
        tui/src/ui/_bridge_tools.py:263,274,289

    Use-case: Allow blocked tool to proceed after user confirmation.
    """
    preview_id: str


@dataclass
class PreviewRejected(Event):
    """
    Published when the user rejects a pending preview.

    Publishers:
        tui/src/ui/_bridge_tools.py:268,280,298

    Use-case: Cancel blocked tool; close preview panel.
    """
    preview_id: str


@dataclass
class PlanRequested(Event):
    """
    Published when the graph wait-for-user node needs the user to approve a plan.

    Publishers:
        src/core/orchestration/graph/nodes/wait_for_user_node.py:66

    Use-case: Render plan approval dialog.
    """
    plan: Any
    blocked_tool: Optional[str]
    session_id: str


@dataclass
class PlanProgress(Event):
    """
    Published for each plan step progress update.

    Publishers:
        src/core/orchestration/graph/nodes/execution_tool.py:26

    Use-case: Update progress bar in TUI.
    """
    plan_progress: Dict[str, Any]


# ---------------------------------------------------------------------------
# Step (graph node progress)
# ---------------------------------------------------------------------------

@dataclass
class StepStart(Event):
    """
    Published at the start of each graph execution step.

    Publishers:
        src/core/orchestration/graph/nodes/execution_tool.py:61

    Use-case: Show step N of M in TUI footer.
    """
    step: int
    total: int
    tool: str
    description: str
    session_id: str


@dataclass
class StepFinish(Event):
    """
    Published at the end of each graph execution step.

    Publishers:
        src/core/orchestration/graph/nodes/execution_tool.py:101

    Use-case: Mark step done; compute elapsed time display.
    """
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
    """
    Published when a new session is created.

    Publishers:
        src/server/app.py:161
        src/server/task_endpoints.py:253

    Use-case: Add session tab to TUI; initialise session state.
    """
    session_id: str
    metadata: Optional[Dict[str, Any]] = None
    task_id: Optional[str] = None


@dataclass
class SessionNew(Event):
    """
    Published from the settings controller when a new blank session is requested.

    Publishers:
        src/core/settings/controller.py:318

    Use-case: Reset chat view; start fresh conversation.
    """


@dataclass
class SessionHydrated(Event):
    """
    Published when an existing session's history is loaded from storage.

    Publishers:
        src/core/orchestration/orchestrator_event_subscriptions.py:109

    Use-case: Populate chat history in TUI; restore scroll position.
    """
    session_id: str
    message_history: List[Dict[str, Any]]
    current_task: Optional[str]
    working_dir: str


@dataclass
class SessionTitleGenerated(Event):
    """
    Published when the inference loop auto-generates a session title.

    Publishers:
        src/core/orchestration/inference_loop.py:153

    Use-case: Update session tab label in TUI.
    """
    title: str


@dataclass
class SessionFilesChanged(Event):
    """
    Published when the workspace file watcher detects changes.

    Publishers:
        src/core/orchestration/session_manager.py:292

    Use-case: Refresh file tree panel; highlight changed files.
    """
    files: List[Dict[str, str]]  # [{path, absolute}, ...]
    workdir: str
    is_git_repo: bool


@dataclass
class SessionRegistered(Event):
    """
    Published when a session is registered with the event bus (mock / dev mode).

    Publishers:
        tui/mock_engine.py (mock mode only)

    Use-case: Track active sessions; update session list widget.
    """
    session_id: str


@dataclass
class SessionUnregistered(Event):
    """
    Published when a session is unregistered from the event bus.

    Use-case: Remove session from active list; clean up resources.
    """
    session_id: str


@dataclass
class SessionHealthAlert(Event):
    """
    Published when a session health check detects an issue.

    Subscribers:
        TUI bridge (session health banner)

    Use-case: Show health-warning in UI; suggest /new session.
    """
    session_id: str
    message: str
    level: Literal["info", "warning", "error"] = "warning"


@dataclass
class SessionRequestState(Event):
    """
    Published from the TUI bridge to request the backend to persist session state.

    Publishers:
        tui/src/ui/_bridge_session.py:259

    Use-case: Trigger session state snapshot from the UI.
    """
    session_id: str


# ---------------------------------------------------------------------------
# Provider / model
# ---------------------------------------------------------------------------

@dataclass
class ProviderStatusChanged(Event):
    """
    Published when a provider's connectivity status changes.

    Publishers:
        src/core/inference/provider_probe.py:123,129,143,197

    Use-case: Update provider status indicator; enable/disable model selector.
    """
    provider: str
    status: str  # "connected" | "disconnected" | "unknown"


@dataclass
class ProviderModelsList(Event):
    """
    Published when a fresh model list is fetched from a provider.

    Publishers:
        src/core/inference/provider_probe.py:115
        src/core/inference/provider_loading.py:118

    Use-case: Populate model dropdown in settings.
    """
    provider: str
    models: List[str]


@dataclass
class ProviderModelsCached(Event):
    """
    Published when the model list is served from cache (no fresh fetch needed).

    Publishers:
        src/core/inference/provider_probe.py:119
        src/core/inference/provider_loading.py:121
        src/core/orchestration/orchestrator_helpers.py:627

    Use-case: Same as ProviderModelsList but signals stale data.
    """
    provider: str
    models: Optional[List[str]] = None


@dataclass
class ProviderModelsEmpty(Event):
    """
    Published when a provider returns an empty model list.

    Publishers:
        src/core/inference/provider_probe.py:128

    Use-case: Show "no models available" warning in settings.
    """
    provider: str


@dataclass
class ProviderModelsUpdated(Event):
    """
    Published from the settings controller with a refreshed model list.

    Publishers:
        src/core/settings/controller.py:161

    Use-case: Refresh model selector widget.
    """
    provider: str
    models: List[str]


@dataclass
class ProviderSelectionChanged(Event):
    """
    Published when the user selects a different provider or model.

    Publishers:
        src/core/settings/controller.py:216

    Use-case: Switch active provider; persist preference.
    """
    provider: str
    model: Optional[str]


@dataclass
class ProviderContextWindow(Event):
    """
    Published after discovering a provider's context-window size.

    Publishers:
        src/core/inference/provider_probe.py:241

    Use-case: Calibrate token-budget calculations.
    """
    provider: str
    model: str
    context_window: int


@dataclass
class ProviderUnavailable(Event):
    """
    Published when the primary provider cannot be initialised at startup.

    Publishers:
        src/core/orchestration/orchestrator_provider_init.py:115

    Use-case: Show fatal startup error; prompt user to fix config.
    """
    reason: str


@dataclass
class ProviderConfigMissing(Event):
    """
    Published when a required provider config file is absent.

    Publishers:
        src/core/inference/llm_manager.py:933

    Use-case: Surface "missing config" banner with actionable path.
    """
    path: str


@dataclass
class ProviderModelMissing(Event):
    """
    Published when the configured model is not in the provider's list.

    Publishers:
        src/core/inference/model_selection.py:255

    Use-case: Warn user; fall back to first available model.
    """
    provider: Optional[str]
    requested: Optional[str]
    available: List[str]


@dataclass
class ProviderLimit(Event):
    """
    Published when the provider returns a rate-limit or quota error.

    Publishers:
        src/core/orchestration/graph/nodes/node_utils.py:152

    Use-case: Show "provider limit reached" warning; back off.
    """
    error: str


# ---------------------------------------------------------------------------
# Inference / streaming
# ---------------------------------------------------------------------------

@dataclass
class ResponseStreamChunk(Event):
    """
    Published for each streaming response chunk from the LLM.

    Publishers:
        src/core/inference/streaming.py:84

    Use-case: Append text to the streaming message widget in TUI.
    """
    chunk: str
    is_reasoning: bool


@dataclass
class ResponseStreamEnd(Event):
    """
    Published when the LLM streaming response is complete.

    Publishers:
        src/core/inference/streaming.py:147

    Use-case: Finalise message widget; enable copy button.
    """
    full_text: str


@dataclass
class ModelToken(Event):
    """
    Published for each token emitted during streaming.

    Publishers:
        src/core/inference/streaming.py:86,145

    Use-case: Token-level streaming display; token counter.
    """
    text: str
    partial: bool
    full: Optional[str] = None  # present when partial=False


@dataclass
class LLMToken(Event):
    """
    Published alongside ModelToken for subscribers that need is_reasoning flag.

    Publishers:
        src/core/inference/streaming.py:87,146

    Use-case: Separate reasoning-token display from normal tokens.
    """
    text: str
    partial: bool
    is_reasoning: bool = False
    full: Optional[str] = None  # present when partial=False


@dataclass
class ModelResponse(Event):
    """
    Published after a full LLM response (telemetry summary).

    Publishers:
        src/core/inference/telemetry.py:40

    Use-case: Latency / token metrics; cost estimation.
    """
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
    """
    Published when the orchestrator selects which model to use.

    Publishers:
        src/core/orchestration/orchestrator_helpers.py:171

    Use-case: Display active model in TUI status bar.
    """
    selected: str
    provider: str
    available_models: List[str]


@dataclass
class ModelRoutingComplete(Event):
    """
    Published after routing is confirmed (post-switch).

    Publishers:
        src/core/inference/llm_manager.py:583

    Use-case: Confirm model switch in settings UI.
    """
    model: str
    provider: Optional[str]
    switched_provider: Optional[str]


# ---------------------------------------------------------------------------
# Context / memory
# ---------------------------------------------------------------------------

@dataclass
class ContextOverflow(Event):
    """
    Published when the prompt exceeds the context window.

    Publishers:
        src/core/orchestration/graph/nodes/perception_node.py:883
        src/core/orchestration/graph/nodes/perception_post_call.py:26,123

    Use-case: Trigger auto-compaction; show warning banner.
    """
    prompt_tokens: int
    budget: int
    reserved: int
    session_id: str
    source: str  # "api_error" | "pre_flight"


@dataclass
class ContextCompacted(Event):
    """
    Published after successful manual compaction.

    Publishers:
        src/core/memory/compaction_service.py:262

    Use-case: Show "context compacted" confirmation; update token display.
    """
    message: str
    method: str
    tokens_before: int
    tokens_after: int


@dataclass
class ContextAutoCompacted(Event):
    """
    Published after automatic compaction triggered by overflow.

    Publishers:
        src/core/orchestration/graph/nodes/perception_compaction.py:132

    Use-case: Silent notification; update token counter.
    """
    method: str
    tokens_before: int
    tokens_after: int
    new_message_count: int
    session_id: str


@dataclass
class ContextDegraded(Event):
    """
    Published when context quality degrades (e.g. repeated truncation).

    Subscribers:
        TUI bridge (context quality warning)

    Use-case: Show "context degraded" warning; suggest compaction.
    """
    session_id: str
    reason: str
    tokens_lost: Optional[int] = None


@dataclass
class ContextCompactFailed(Event):
    """
    Published when compaction fails.

    Publishers:
        src/core/memory/compaction_service.py:272
        src/core/orchestration/orchestrator_helpers.py:257

    Use-case: Show error banner; let user decide next action.
    """
    message: str


@dataclass
class MessageTruncation(Event):
    """
    Published when old messages are dropped to fit context.

    Publishers:
        src/core/orchestration/message_manager.py:236

    Use-case: Show "X messages dropped" notice in chat.
    """
    dropped_count: int
    dropped_tokens: int
    tokens_after: int


@dataclass
class MessageCompactionApplied(Event):
    """
    Published when the scheduler applies message compaction.

    Publishers:
        src/core/orchestration/orchestrator_event_subscriptions.py:188

    Use-case: Audit log; token-savings dashboard.
    """
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
    """
    Published with a human-readable token budget summary.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:912

    Use-case: Display "used X% of budget" warning in footer.
    """
    used: int
    limit: int
    percent: float
    warning: bool


@dataclass
class TokenBudgetUpdate(Event):
    """
    Published with raw token counts for metrics.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:900

    Use-case: Update token-usage graph; trigger compaction if near limit.
    """
    used_tokens: int
    max_tokens: int
    usage_ratio: float
    session_id: str


@dataclass
class UsageTurnSummary(Event):
    """
    Published at the end of each LLM turn with cost and token breakdown.

    Publishers:
        src/core/orchestration/session_cost_tracker.py:373

    Use-case: Running cost display; per-turn analytics.
    """
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
    """
    Published when session cost exceeds the configured budget ceiling.

    Publishers:
        src/core/orchestration/session_cost_tracker.py:395

    Use-case: Show budget-exceeded error; optionally stop execution.
    """
    session_cost_usd: float
    budget_ceiling_usd: float


@dataclass
class TokenBudgetWarning(Event):
    """
    Published when token usage approaches the configured limit.

    Subscribers:
        TUI bridge (token warning banner)

    Use-case: Warning-level notification to user; suggest compaction.
    """
    used: int
    limit: int
    percent: float
    message: Optional[str] = None


@dataclass
class UsageSubagentCost(Event):
    """
    Published per sub-agent turn with its cost and role.

    Publishers:
        src/tools/subagent_tools.py:404

    Subscribers:
        TUI bridge (sub-agent cost breakdown)

    Use-case: Accumulate sub-agent costs for session total.
    """
    child_session_id: str
    role: str
    cost_usd: float


# ---------------------------------------------------------------------------
# File system
# ---------------------------------------------------------------------------

@dataclass
class FileModified(Event):
    """
    Published when a tool modifies a file.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:836

    Use-case: Refresh file tree; highlight modified file.
    """
    path: str
    tool: str
    workdir: str


@dataclass
class FileDeleted(Event):
    """
    Published when a tool deletes a file.

    Publishers:
        src/core/orchestration/tool_execution_pipeline.py:854

    Use-case: Remove file from file tree; update session state.
    """
    path: str
    workdir: str


@dataclass
class FileDiffPreview(Event):
    """
    Published when a diff preview is ready for user review.

    Publishers:
        src/tools/_diff_gate.py:189

    Subscribers:
        TUI bridge (diff preview panel)

    Use-case: Show side-by-side diff; wait for user accept/reject.
    """
    path: str
    diff: str
    is_new_file: bool = False


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------

@dataclass
class DelegationStart(Event):
    """
    Published when a new sub-agent delegation begins.

    Publishers:
        src/tools/subagent_tools.py:345

    Subscribers:
        TUI bridge (sub-agent progress widget)

    Use-case: Show sub-agent start in UI; track delegation chain.
    """
    child_session_id: str
    parent_session_id: str
    role: str
    task: str


@dataclass
class DelegationFinish(Event):
    """
    Published when a sub-agent delegation completes.

    Publishers:
        src/tools/subagent_tools.py:384

    Subscribers:
        TUI bridge (sub-agent finish indicator)

    Use-case: Mark sub-agent done; update cost display.
    """
    child_session_id: str
    role: str
    ok: bool
    cost_usd: float


@dataclass
class DelegationComplete(Event):
    """
    Published when all delegated sub-agents have finished.

    Publishers:
        src/core/orchestration/graph/nodes/delegation_node.py:391

    Use-case: Resume parent agent; aggregate sub-agent results.
    """
    count: int
    keys: List[str]
    session_id: str


@dataclass
class AgentScoutFilesDiscovered(Event):
    """
    Published by a scout sub-agent when it has discovered relevant files.

    Publishers:
        src/core/orchestration/graph/nodes/delegation_node.py:85

    Use-case: Show discovered file list; feed into next analysis step.
    """
    files: List[str]
    agent_id: str
    result: Any


@dataclass
class AgentResearcherDocSummary(Event):
    """
    Published by a researcher sub-agent with a doc summary.

    Publishers:
        src/core/orchestration/graph/nodes/delegation_node.py:90

    Use-case: Accumulate doc summaries for synthesis.
    """
    summary: str   # truncated to 500 chars at publish site
    agent_id: str


@dataclass
class AgentReviewerBugFound(Event):
    """
    Published by a reviewer sub-agent with discovered bugs.

    Publishers:
        src/core/orchestration/graph/nodes/delegation_node.py:95

    Use-case: Aggregate bugs; surface in review report.
    """
    bugs: List[Any]
    agent_id: str
    result: Any


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

@dataclass
class SchedulerDistillRequest(Event):
    """
    Published when the scheduler requests memory distillation.

    Publishers:
        src/core/orchestration/orchestrator_scheduler.py:28

    Use-case: Trigger background distillation worker.
    """
    source: str
    time: float


@dataclass
class SchedulerDistillCompleted(Event):
    """
    Published when distillation has completed.

    Publishers:
        src/core/orchestration/orchestrator_event_subscriptions.py:208

    Use-case: Signal to scheduler that memory is fresh.
    """
    source: str


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

@dataclass
class McpServerStatus(Event):
    """
    Published when an MCP server starts, stops, or changes state.

    Publishers:
        src/core/orchestration/orchestrator_helpers.py:756,765
        src/core/orchestration/mcp_stdio_server.py:604,641,658,690
        src/core/mcp/manager.py:130

    Use-case: Show MCP server indicator in TUI status bar.
    """
    running: bool
    count: int
    has_error: bool = False
    server_names: List[str] = field(default_factory=list)
    # manager.py passes a rich dict; normalise to flat fields where possible
    servers: Optional[Dict[str, Any]] = None


@dataclass
class McpToolsListChanged(Event):
    """
    Published when the list of tools from an MCP server changes.

    Publishers:
        src/core/mcp/manager.py:147

    Use-case: Refresh tool palette; revalidate tool schemas.
    """
    server: str
    params: Any


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ConfigReloaded(Event):
    """
    Published when configuration files are reloaded at runtime.

    Publishers:
        src/core/orchestration/orchestrator_config_reload.py:104
        src/core/config_loader.py:442

    Use-case: Re-apply settings without restart.
    """
    changed_paths: List[str]


# ---------------------------------------------------------------------------
# Orchestrator lifecycle
# ---------------------------------------------------------------------------

@dataclass
class OrchestratorStartup(Event):
    """
    Published when the orchestrator finishes initialisation.

    Publishers:
        src/core/orchestration/orchestrator_provider_init.py:97,105

    Use-case: Show "ready" status; enable input field.
    """
    time: float
    working_dir: str


@dataclass
class OrchestratorModelsCheckStarted(Event):
    """
    Published when orchestrator starts probing provider models.

    Publishers:
        src/core/orchestration/orchestrator_event_subscriptions.py:65

    Use-case: Show loading indicator in model selector.
    """
    payload: Dict[str, Any]


@dataclass
class OrchestratorModelsCheckCompleted(Event):
    """
    Published when orchestrator finishes probing provider models (success).

    Publishers:
        src/core/orchestration/orchestrator_event_subscriptions.py:72

    Use-case: Hide loading indicator; enable model selector.
    """
    payload: Dict[str, Any]


@dataclass
class OrchestratorModelsCheckFailed(Event):
    """
    Published when orchestrator model probe fails.

    Publishers:
        src/core/orchestration/orchestrator_event_subscriptions.py:79

    Use-case: Show error; prompt user to check provider settings.
    """
    payload: Dict[str, Any]


# ---------------------------------------------------------------------------
# UI / notifications
# ---------------------------------------------------------------------------

@dataclass
class UiNotification(Event):
    """
    Published to surface a human-readable notification in the TUI.

    Publishers:
        src/core/orchestration/orchestrator_event_subscriptions.py:17,31,45
        src/core/orchestration/graph/nodes/perception_runtime.py:347

    Use-case: Toast / banner in TUI; error or warning level.
    """
    level: Literal["info", "warning", "error"]
    message: str
    source: Optional[str] = None


@dataclass
class HookMessage(Event):
    """
    Published by shell hooks with a user-visible message.

    Publishers:
        src/core/orchestration/shell_hooks.py:490

    Use-case: Display hook output in TUI tool area.
    """
    tool_name: str
    event: str
    message: str


@dataclass
class GitBranch(Event):
    """
    Published with git status for the working directory.

    Publishers:
        src/core/orchestration/orchestrator_helpers.py:735

    Use-case: Show branch name / dirty indicator in TUI footer.
    """
    branch: str
    dirty: bool
    ahead: int
    behind: int


@dataclass
class WorkingDirUnavailable(Event):
    """
    Published when the configured working directory is inaccessible.

    Publishers:
        src/core/orchestration/orchestrator_helpers.py:538

    Use-case: Show "working directory unavailable" error; block input.
    """
    path: str
    error: str


@dataclass
class SystemSettings(Event):
    """
    Published from the TUI bridge with resolved configuration values.

    Publishers:
        tui/src/ui/_bridge_provider.py:86

    Subscribers:
        TUI app (apply settings to UI state)

    Use-case: Seed UI state with backend config after startup.
    """
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
    """
    Published for each log line emitted by the core logger.

    Publishers:
        src/core/logger.py:173

    Subscribers:
        TUI bridge (log panel / debug view)

    Use-case: Display real-time log stream in TUI debug panel.
    """
    level: str
    message: str
    logger: Optional[str] = None


@dataclass
class RoleTransition(Event):
    """
    Published when the agent's role changes (e.g. coding → planning).

    Publishers:
        src/tools/role_tools.py:43  (published as ``role.changed``)

    Subscribers:
        TUI bridge (role display in footer)

    Use-case: Update role indicator; re-render role-specific UI.
    """
    role: str


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

@dataclass
class RetryAttempt(Event):
    """
    Published when a retry is attempted after a failure.

    Subscribers:
        TUI bridge (retry indicator in chat)

    Use-case: Show retry countdown / attempt number.
    """
    attempt: int
    max_attempts: int
    reason: str


@dataclass
class RetrySucceeded(Event):
    """
    Published when a retry attempt succeeds.

    Subscribers:
        TUI bridge (clear retry indicator)

    Use-case: Remove retry warning; show success.
    """
    attempt: int
    reason: str


@dataclass
class RetryFailed(Event):
    """
    Published when all retry attempts are exhausted.

    Subscribers:
        TUI bridge (show permanent failure)

    Use-case: Show "retry exhausted" error; offer abort options.
    """
    attempt: int
    max_attempts: int
    reason: str


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@dataclass
class TaskQueueUpdated(Event):
    """
    Published when the pending task queue changes.

    Subscribers:
        TUI bridge (task queue count in footer)

    Use-case: Update pending-task badge; show queue depth.
    """
    pending_tasks: int
    queue_size: int
    session_id: str


# ---------------------------------------------------------------------------
# Perception
# ---------------------------------------------------------------------------

@dataclass
class PerceptionCorrectivePrompt(Event):
    """
    Published when the perception node injects a corrective prompt.

    Publishers:
        src/core/orchestration/graph/nodes/perception_no_tool.py:74

    Use-case: Debug log; metrics on model correction frequency.
    """
    session_id: str
    attempt: int
    reason: str
    model_tier: str
    truncated_yaml: str


@dataclass
class TaskTurnLimit(Event):
    """
    Published when the task hits its maximum turn count.

    Publishers:
        src/core/orchestration/graph/nodes/perception_runtime.py:58

    Use-case: Show "turn limit reached" notice; stop execution.
    """
    turn_count: int
    max_turns: int


# ---------------------------------------------------------------------------
# Subagent dispatch
# ---------------------------------------------------------------------------

@dataclass
class SubagentDispatch(Event):
    """
    Published when a sub-agent is dispatched via the cross-session bus.

    Publishers:
        src/core/orchestration/event_bus.py (publish_dispatch helper)

    Use-case: Track delegation chain; update sub-agent status list.
    """
    delegation_key: str
    role: str
    task: str


@dataclass
class SubagentResult(Event):
    """
    Published when a sub-agent returns its result via the cross-session bus.

    Publishers:
        src/core/orchestration/event_bus.py (publish_dispatch_result helper)

    Use-case: Collect sub-agent results; trigger parent resume.
    """
    delegation_key: str
    role: str
    task: str
    result: Any
