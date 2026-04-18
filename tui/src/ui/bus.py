from textual.message import Message
from typing import Dict, Any, Optional


# ── Existing events (kept for compatibility) ─────────────────────────────────


class StreamChunkEvent(Message):
    def __init__(
        self, chunk: str, role: str = "", run_id: str = "", is_partial: bool = True
    ) -> None:
        self.chunk = chunk
        self.role = role
        self.run_id = run_id
        self.is_partial = is_partial
        super().__init__()


class DisplayReasoning(Message):
    def __init__(self, content: str, start_time: float) -> None:
        self.content = content
        self.start_time = start_time
        super().__init__()


class StreamingThinkingUpdate(Message):
    def __init__(self, content: str, is_complete: bool = False) -> None:
        self.content = content
        self.is_complete = is_complete
        super().__init__()


class StatusUpdate(Message):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__()


class ToolExecutionNotice(Message):
    def __init__(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> None:
        self.tool_name = tool_name
        self.arguments = arguments or {}
        super().__init__()


class UpdateSidebarData(Message):
    def __init__(self, pending_tasks: int, log_entries: int) -> None:
        self.pending_tasks = pending_tasks
        self.log_entries = log_entries
        super().__init__()


class AgentFinalResponse(Message):
    def __init__(self, content: str) -> None:
        self.content = content
        super().__init__()


class WorkerError(Message):
    def __init__(self, message: str, traceback: str = "") -> None:
        self.message = message
        self.traceback = traceback
        super().__init__()


class RoleTransitionEvent(Message):
    def __init__(self, from_role: str, to_role: str, run_id: str = "") -> None:
        self.from_role = from_role
        self.to_role = to_role
        self.run_id = run_id
        super().__init__()


class TokenUsageEvent(Message):
    def __init__(
        self,
        system: int = 0,
        task: int = 0,
        tools: int = 0,
        total: int = 0,
        model_window: int = 0,
        system_tokens: int = 0,
        task_tokens: int = 0,
        tool_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        self.system = system or system_tokens
        self.task = task or task_tokens
        self.tools = tools or tool_tokens
        self.total = total or total_tokens
        self.model_window = model_window
        self.system_tokens = system_tokens
        self.task_tokens = task_tokens
        self.tool_tokens = tool_tokens
        self.total_tokens = total_tokens
        super().__init__()


class TaskQueueUpdatedEvent(Message):
    def __init__(
        self,
        queue_size: int = 0,
        pending_count: int = 0,
        task_id: Optional[str] = None,
        old_status: Optional[str] = None,
        new_status: Optional[str] = None,
        run_id: str = "",
    ) -> None:
        self.queue_size = queue_size
        self.pending_count = pending_count
        self.task_id = task_id
        self.old_status = old_status
        self.new_status = new_status
        self.run_id = run_id
        super().__init__()


class FileModifiedEvent(Message):
    def __init__(self, file_path: str = "", diff: str = "", run_id: str = "") -> None:
        self.file_path = file_path
        self.diff = diff
        self.run_id = run_id
        super().__init__()


class TaskEscalatedEvent(Message):
    def __init__(
        self,
        task_id: str = "",
        reason: str = "",
        retry_count: int = 0,
        run_id: str = "",
    ) -> None:
        self.task_id = task_id
        self.reason = reason
        self.retry_count = retry_count
        self.run_id = run_id
        super().__init__()


class ContextDegradedEvent(Message):
    def __init__(self, target_window: int = 0, reason: str = "") -> None:
        self.target_window = target_window
        self.reason = reason
        super().__init__()


class RetryAttemptEvent(Message):
    def __init__(
        self,
        attempt_number: int = 0,
        max_attempts: int = 0,
        error_type: str = "",
        provider: str = "",
        run_id: str = "",
    ) -> None:
        self.attempt_number = attempt_number
        self.max_attempts = max_attempts
        self.error_type = error_type
        self.provider = provider
        self.run_id = run_id
        super().__init__()


class RetrySucceededEvent(Message):
    def __init__(
        self, attempt_number: int = 0, provider: str = "", run_id: str = ""
    ) -> None:
        self.attempt_number = attempt_number
        self.provider = provider
        self.run_id = run_id
        super().__init__()


class RetryFailedEvent(Message):
    def __init__(
        self,
        total_attempts: int = 0,
        error_type: str = "",
        provider: str = "",
        run_id: str = "",
    ) -> None:
        self.total_attempts = total_attempts
        self.error_type = error_type
        self.provider = provider
        self.run_id = run_id
        super().__init__()


class ProviderStatusChangeEvent(Message):
    def __init__(
        self,
        provider: str = "",
        old_status: str = "",
        new_status: str = "",
        run_id: str = "",
    ) -> None:
        self.provider = provider
        self.old_status = old_status
        self.new_status = new_status
        self.run_id = run_id
        super().__init__()


class SystemSettingsLoaded(Message):
    """Backend sends the current configuration to the UI on startup."""

    def __init__(
        self, settings_dict: Dict[str, Any], available_providers: list
    ) -> None:
        self.settings = settings_dict
        self.providers = available_providers
        super().__init__()


# ── New spec-compliant events ─────────────────────────────────────────────────


class ToolCallStartEvent(Message):
    """§4.5 tool.execute.start — tool call began (in-progress indicator)."""

    def __init__(
        self, tool_name: str, tool_args: Dict[str, Any], tool_id: str = ""
    ) -> None:
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_id = tool_id
        super().__init__()


class ToolCallFinishEvent(Message):
    """§4.5 tool.execute.finish — formatted result ready."""

    def __init__(
        self, tool_name: str, tool_id: str, result_text: str, ok: bool = True
    ) -> None:
        self.tool_name = tool_name
        self.tool_id = tool_id
        self.result_text = result_text
        self.ok = ok
        super().__init__()


class ToolCallErrorEvent(Message):
    """§4.5 tool.execute.error — tool call failed."""

    def __init__(self, tool_name: str, tool_id: str, error: str) -> None:
        self.tool_name = tool_name
        self.tool_id = tool_id
        self.error = error
        super().__init__()


class DiffPreviewEvent(Message):
    """§4.5 file.diff.preview — show diff BEFORE write completes."""

    def __init__(
        self,
        path: str,
        diff: str,
        is_new_file: bool = False,
        requires_confirmation: bool = False,
    ) -> None:
        self.path = path
        self.diff = diff
        self.is_new_file = is_new_file
        self.requires_confirmation = requires_confirmation
        super().__init__()


class PlanProgressEvent(Message):
    """§4.5 plan.progress — step N of M."""

    def __init__(self, step: int, total: int, description: str) -> None:
        self.step = step
        self.total = total
        self.description = description
        super().__init__()


class PlanRequestedEvent(Message):
    """§4.5 plan.requested — agent waiting for plan approval."""

    def __init__(self, plan_text: str = "") -> None:
        self.plan_text = plan_text
        super().__init__()


class TokenBudgetEvent(Message):
    """§4.5 token.budget.update / token.budget.warning."""

    def __init__(
        self, used: int, limit: int, percent: float, warning: bool = False
    ) -> None:
        self.used = used
        self.limit = limit
        self.percent = percent
        self.warning = warning
        super().__init__()


class BashApprovalEvent(Message):
    """§16.1 bash tier-3 command requires user approval."""

    def __init__(self, tool_id: str, command: str) -> None:
        self.tool_id = tool_id
        self.command = command
        super().__init__()


class NotificationEvent(Message):
    """§4.5 ui.notification — show as toast or inline banner."""

    def __init__(self, level: str, message: str, source: str = "") -> None:
        self.level = level
        self.message = message
        self.source = source
        super().__init__()


class SessionHealthEvent(Message):
    """§4.5 session.health_alert."""

    def __init__(self, level: str, title: str, message: str) -> None:
        self.level = level
        self.title = title
        self.message = message
        super().__init__()


class ModelRoutingEvent(Message):
    """§4.5 model.routing — active provider/model changed."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        super().__init__()


class OrchestratorReadyEvent(Message):
    """§4.5 orchestrator.startup — core services ready."""

    def __init__(self, working_dir: str) -> None:
        self.working_dir = working_dir
        super().__init__()


class AgentRunningEvent(Message):
    """Internal — agent started or stopped; used to gate the input field."""

    def __init__(self, running: bool) -> None:
        self.running = running
        super().__init__()


class SubagentStartEvent(Message):
    """delegation.start — a subagent was spawned."""

    def __init__(
        self,
        child_session_id: str,
        role: str,
        task: str,
        parent_session_id: str | None = None,
    ) -> None:
        self.child_session_id = child_session_id
        self.role = role
        self.task = task
        self.parent_session_id = parent_session_id
        super().__init__()


class SubagentFinishEvent(Message):
    """delegation.finish — a subagent completed or failed."""

    def __init__(self, child_session_id: str, role: str, ok: bool) -> None:
        self.child_session_id = child_session_id
        self.role = role
        self.ok = ok
        super().__init__()


class GitBranchEvent(Message):
    """git.branch — current branch name and working-tree state."""

    def __init__(
        self,
        branch: str,
        dirty: bool = False,
        ahead: int = 0,
        behind: int = 0,
    ) -> None:
        self.branch = branch
        self.dirty = dirty
        self.ahead = ahead
        self.behind = behind
        super().__init__()


class StepStartEvent(Message):
    """step.start — an agent step began (opencode-style step boundary)."""

    def __init__(
        self, tool: str = "", step: int = 0, total: int = 0, run_id: str = ""
    ) -> None:
        self.tool = tool
        self.step = step
        self.total = total
        self.run_id = run_id
        super().__init__()


class StepFinishEvent(Message):
    """step.finish — an agent step completed."""

    def __init__(
        self,
        tool: str = "",
        ok: bool = True,
        elapsed_ms: Optional[int] = None,
        run_id: str = "",
    ) -> None:
        self.tool = tool
        self.ok = ok
        self.elapsed_ms = elapsed_ms
        self.run_id = run_id
        super().__init__()


class McpServerStatusEvent(Message):
    """mcp.server.status — MCP server started/stopped."""

    def __init__(
        self,
        running: bool = False,
        count: int = 0,
        server_names: Optional[list] = None,
        has_error: bool = False,
    ) -> None:
        self.running = running
        self.count = count
        self.server_names = server_names or []
        self.has_error = has_error  # GAP-FOOTER-2: True when any server has an error
        super().__init__()


class ToolPermissionEvent(Message):
    """tool.permission_required — agent needs user approval to run a destructive tool."""

    def __init__(
        self, tool: str = "", args: Optional[Dict[str, Any]] = None, tool_id: str = ""
    ) -> None:
        self.tool = tool
        self.args = args or {}
        self.tool_id = tool_id
        super().__init__()


class UsageTurnSummaryEvent(Message):
    """usage.turn_summary — per-turn token counts and estimated cost (TUI-T6)."""

    def __init__(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "",
        cost_usd: float = 0.0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model
        self.cost_usd = cost_usd
        super().__init__()


class DoomLoopEvent(Message):
    """tool.doom_loop_detected — agent is repeating identical tool calls (PERM-W3)."""

    def __init__(
        self,
        tool_name: str = "",
        fingerprint: str = "",
        count: int = 3,
        tool_id: str = "",
    ) -> None:
        self.tool_name = tool_name
        self.fingerprint = fingerprint
        self.count = count
        self.tool_id = tool_id
        super().__init__()


# ── GitHub Copilot OAuth device flow events ───────────────────────────────────


class DeviceFlowStartEvent(Message):
    """Fired when the GitHub device flow has been initiated.

    The UI should show the user_code and open (or display) the verification_uri
    while polling for the token in the background.
    """

    def __init__(
        self,
        user_code: str,
        verification_uri: str,
        expires_in: int = 900,
    ) -> None:
        self.user_code = user_code
        self.verification_uri = verification_uri
        self.expires_in = expires_in
        super().__init__()


class DeviceFlowCompleteEvent(Message):
    """Fired when the OAuth token has been received and saved successfully."""

    def __init__(self, provider_id: str = "github_copilot") -> None:
        self.provider_id = provider_id
        super().__init__()


class DeviceFlowErrorEvent(Message):
    """Fired when the device flow fails (expired, cancelled, network error, etc.)."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__()


class ContextCompactedEvent(Message):
    """Fired when the agent context is automatically compacted (TASK-TUI-9).

    Triggers a visual compaction divider in the chat log so the user can see
    where long-term memory was summarised.
    """

    def __init__(self, message: str = "Context compacted") -> None:
        self.message = message
        super().__init__()
