"""
Tests for event_types.py — typed event classes (Phase 2).

Each test is labelled with the USE CASE it validates.  The table below maps
every test to a domain, use-case description, and the publish site(s) it
covers.

USE-CASE TABLE
==============
UC-AG-01  Agent task starts          AgentStart                  task_endpoints.py:154
UC-AG-02  Agent status visible       AgentStatus (working/idle)  frontier_loop_node.py:837,1005
UC-AG-03  Agent task ends            AgentEnd (success/fail)     task_endpoints.py:211
UC-AG-04  Execution mode switches    AgentModeChanged            tool_execution_pipeline.py:749
UC-AG-05  Multi-step plan committed  AgentPlanCommitted          tool_execution_pipeline.py:759
UC-AG-06  Agent status message       AgentMessage                interaction_tools.py:229
UC-AG-07  Agent asks user question   AgentWaitingForUser         interaction_tools.py:66

UC-TL-01  Tool execution starts      ToolExecuteStart            tool_execution_pipeline.py:1034
UC-TL-02  Tool dispatched            ToolInvoked                 tool_execution_pipeline.py:804
UC-TL-03  Tool completes             ToolExecuteFinish           tool_execution_pipeline.py:880
UC-TL-04  Tool fails                 ToolExecuteError            tool_execution_pipeline.py:673
UC-TL-05  Tool result captured       ToolResult                  frontier_loop_node.py:710
UC-TL-06  Tool needs approval        ToolPermissionRequired      permission_gateway.py:701
UC-TL-07  Doom-loop detected         ToolDoomLoopDetected        loop_guards.py:401

UC-PM-01  Spawn needs approval       SpawnPermissionRequired     permission_gateway.py:685
UC-PM-02  Bash command approved      BashApprovalGranted         _bridge_tools.py:272
UC-PM-03  Bash command denied        BashApprovalDenied          _bridge_tools.py:278

UC-PV-01  Plan preview pending       PreviewPending              tool_execution_pipeline.py:928
UC-PV-02  Preview accepted           PreviewConfirmed            _bridge_tools.py:263
UC-PV-03  Preview rejected           PreviewRejected             _bridge_tools.py:268
UC-PV-04  Plan awaits approval       PlanRequested               wait_for_user_node.py:66
UC-PV-05  Plan step progress         PlanProgress                execution_tool.py:26

UC-ST-01  Step starts                StepStart                   execution_tool.py:61
UC-ST-02  Step finishes              StepFinish                  execution_tool.py:101

UC-SE-01  Session created            SessionCreated              app.py:161 / task_endpoints.py:253
UC-SE-02  New blank session          SessionNew                  settings/controller.py:318
UC-SE-03  Session history loaded     SessionHydrated             orchestrator_event_subscriptions.py:109
UC-SE-04  Auto-title generated       SessionTitleGenerated       inference_loop.py:153
UC-SE-05  Workspace files changed    SessionFilesChanged         session_manager.py:292
UC-SE-06  Session registered         SessionRegistered           mock_engine.py (mock mode)
UC-SE-07  Session unregistered       SessionUnregistered         (future)
UC-SE-08  Session health alert       SessionHealthAlert          (future)
UC-SE-09  Session state requested    SessionRequestState         _bridge_session.py:259

UC-PR-01  Provider connects/drops    ProviderStatusChanged       provider_probe.py:123,129,143,197
UC-PR-02  Fresh model list           ProviderModelsList          provider_probe.py:115
UC-PR-03  Cached model list          ProviderModelsCached        provider_probe.py:119
UC-PR-04  No models available        ProviderModelsEmpty         provider_probe.py:128
UC-PR-05  Model list refreshed       ProviderModelsUpdated       settings/controller.py:161
UC-PR-06  Provider/model selected    ProviderSelectionChanged    settings/controller.py:216
UC-PR-07  Context window discovered  ProviderContextWindow       provider_probe.py:241
UC-PR-08  Provider down at startup   ProviderUnavailable         orchestrator_provider_init.py:115
UC-PR-09  Config file missing        ProviderConfigMissing       llm_manager.py:933
UC-PR-10  Configured model missing   ProviderModelMissing        model_selection.py:255
UC-PR-11  Rate-limit hit             ProviderLimit               node_utils.py:152

UC-IN-01  Stream chunk arrives       ResponseStreamChunk         streaming.py:84
UC-IN-02  Stream complete            ResponseStreamEnd           streaming.py:147
UC-IN-03  Partial token emitted      ModelToken (partial)        streaming.py:86
UC-IN-04  Final token emitted        ModelToken (final)          streaming.py:145
UC-IN-05  LLM token with reasoning   LLMToken                    streaming.py:87,146
UC-IN-06  Full response telemetry    ModelResponse               telemetry.py:40
UC-IN-07  Model selected for turn    ModelRouting                orchestrator_helpers.py:171
UC-IN-08  Provider switch confirmed  ModelRoutingComplete        llm_manager.py:583

UC-CM-01  Context overflows          ContextOverflow             perception_post_call.py:123
UC-CM-02  Manual compaction done     ContextCompacted            compaction_service.py:262
UC-CM-03  Auto-compaction done       ContextAutoCompacted        perception_compaction.py:132
UC-CM-04  Compaction failed          ContextCompactFailed        compaction_service.py:272
UC-CM-05  Context degraded           ContextDegraded             (future)
UC-CM-06  Messages dropped           MessageTruncation           message_manager.py:236
UC-CM-07  Scheduler compaction done  MessageCompactionApplied    orchestrator_event_subscriptions.py:188

UC-TB-01  Budget summary             TokenBudget                 tool_execution_pipeline.py:912
UC-TB-02  Budget raw update          TokenBudgetUpdate           tool_execution_pipeline.py:900
UC-TB-03  Budget warning             TokenBudgetWarning          (future)
UC-TB-04  Turn cost summary          UsageTurnSummary            session_cost_tracker.py:373
UC-TB-05  Budget ceiling exceeded    UsageBudgetExceeded         session_cost_tracker.py:395
UC-TB-06  Sub-agent cost             UsageSubagentCost           subagent_tools.py:404

UC-FS-01  File modified by tool      FileModified                tool_execution_pipeline.py:836
UC-FS-02  File deleted by tool       FileDeleted                 tool_execution_pipeline.py:854
UC-FS-03  File diff preview          FileDiffPreview             _diff_gate.py:189

UC-DL-01  Delegation started         DelegationStart             subagent_tools.py:345
UC-DL-02  Delegation finished        DelegationFinish            subagent_tools.py:384
UC-DL-03  Delegation complete        DelegationComplete          delegation_node.py:391
UC-DL-04  Scout discovers files      AgentScoutFilesDiscovered   delegation_node.py:85
UC-DL-05  Researcher summarises doc  AgentResearcherDocSummary   delegation_node.py:90
UC-DL-06  Reviewer finds bugs        AgentReviewerBugFound       delegation_node.py:95

UC-SC-01  Distillation requested     SchedulerDistillRequest     orchestrator_scheduler.py:28
UC-SC-02  Distillation complete      SchedulerDistillCompleted   orchestrator_event_subscriptions.py:208

UC-MC-01  MCP server status          McpServerStatus             mcp_stdio_server.py / manager.py
UC-MC-02  MCP tool list changed      McpToolsListChanged         mcp/manager.py:147

UC-CF-01  Config hot-reloaded        ConfigReloaded              orchestrator_config_reload.py:104
UC-CF-02  System settings loaded     SystemSettings              _bridge_provider.py:86

UC-OR-01  Orchestrator ready         OrchestratorStartup         orchestrator_provider_init.py:97
UC-OR-02  Model probe started        OrchestratorModelsCheckStarted   event_subscriptions.py:65
UC-OR-03  Model probe succeeded      OrchestratorModelsCheckCompleted event_subscriptions.py:72
UC-OR-04  Model probe failed         OrchestratorModelsCheckFailed    event_subscriptions.py:79

UC-UI-01  Warning/error notification UiNotification              orchestrator_event_subscriptions.py:17
UC-UI-02  Hook message               HookMessage                 shell_hooks.py:490
UC-UI-03  Log entry                  LogEntry                    logger.py:173
UC-UI-04  Git branch status          GitBranch                   orchestrator_helpers.py:735
UC-UI-05  Working dir inaccessible   WorkingDirUnavailable       orchestrator_helpers.py:538

UC-RL-01  Role changed               RoleTransition              role_tools.py:43

UC-RT-01  Retry attempt              RetryAttempt                (future)
UC-RT-02  Retry succeeded            RetrySucceeded              (future)
UC-RT-03  Retry failed               RetryFailed                 (future)

UC-TQ-01  Task queue updated         TaskQueueUpdated            (future)

UC-PC-01  Corrective prompt injected PerceptionCorrectivePrompt  perception_no_tool.py:74
UC-PC-02  Turn limit reached         TaskTurnLimit               perception_runtime.py:58

UC-SD-01  Sub-agent dispatched       SubagentDispatch            event_bus.py (publish_dispatch)
UC-SD-02  Sub-agent result returned  SubagentResult              event_bus.py (publish_dispatch_result)

UC-SER-01 Round-trip serialisation   all events (to_dict / from_dict)
UC-SER-02 Inherited fields present   all events (correlation_id, timestamp)
"""

import time

import pytest

from src.core.messaging.event_types import (
    # Agent lifecycle
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
    # Permission / approval
    BashApprovalDenied,
    BashApprovalGranted,
    # Config
    ConfigReloaded,
    # Context / memory
    ContextAutoCompacted,
    ContextCompactFailed,
    ContextCompacted,
    ContextDegraded,
    ContextOverflow,
    # Delegation
    DelegationComplete,
    DelegationFinish,
    DelegationStart,
    # File system
    FileDeleted,
    FileDiffPreview,
    FileModified,
    # Git / UI
    GitBranch,
    # Hook
    HookMessage,
    # Streaming / inference
    LLMToken,
    # Logging
    LogEntry,
    # MCP
    McpServerStatus,
    McpToolsListChanged,
    MessageCompactionApplied,
    # Context continued
    MessageTruncation,
    ModelResponse,
    ModelRouting,
    ModelRoutingComplete,
    ModelToken,
    # Orchestrator
    OrchestratorModelsCheckCompleted,
    OrchestratorModelsCheckFailed,
    OrchestratorModelsCheckStarted,
    OrchestratorStartup,
    # Perception
    PerceptionCorrectivePrompt,
    PlanProgress,
    # Preview / plan
    PlanRequested,
    PreviewConfirmed,
    PreviewPending,
    PreviewRejected,
    # Provider
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
    # Retry
    RetryAttempt,
    RetryFailed,
    RetrySucceeded,
    # Role
    RoleTransition,
    # Scheduler
    SchedulerDistillCompleted,
    SchedulerDistillRequest,
    # Session
    SessionCreated,
    SessionFilesChanged,
    SessionHealthAlert,
    SessionHydrated,
    SessionNew,
    SessionRegistered,
    SessionRequestState,
    SessionTitleGenerated,
    SessionUnregistered,
    # Spawn
    SpawnPermissionRequired,
    # Steps
    StepFinish,
    StepStart,
    # Subagent
    SubagentDispatch,
    SubagentResult,
    # Config / settings
    SystemSettings,
    # Task
    TaskQueueUpdated,
    TaskTurnLimit,
    # Token / usage
    TokenBudget,
    TokenBudgetUpdate,
    TokenBudgetWarning,
    # Tool execution
    ToolDoomLoopDetected,
    ToolExecuteError,
    ToolExecuteFinish,
    ToolExecuteStart,
    ToolInvoked,
    ToolPermissionRequired,
    ToolResult,
    # UI notification
    UiNotification,
    UsageBudgetExceeded,
    UsageSubagentCost,
    UsageTurnSummary,
    WorkingDirUnavailable,
)
from src.core.messaging.events import Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_trip(event: Event) -> Event:
    """Serialise to dict and back."""
    return type(event).from_dict(event.to_dict())


def _has_base_fields(event: Event) -> None:
    """Assert that base Event fields are auto-populated."""
    assert event.correlation_id, "correlation_id must be non-empty"
    assert event.timestamp > 0, "timestamp must be positive"


# ---------------------------------------------------------------------------
# UC-AG — Agent lifecycle
# ---------------------------------------------------------------------------

class TestAgentLifecycle:
    """Use-cases: UC-AG-01 … UC-AG-05"""

    def test_uc_ag_01_agent_task_starts(self):
        """UC-AG-01: An agent task begins — verify all required fields present."""
        e = AgentStart(task_id="t1", session_id="s1", task="Fix login bug")
        _has_base_fields(e)
        assert e.task_id == "t1"
        assert e.session_id == "s1"
        assert e.task == "Fix login bug"

    def test_uc_ag_01_round_trip(self):
        """UC-AG-01: AgentStart survives serialisation round-trip."""
        original = AgentStart(task_id="t1", session_id="s1", task="Fix login bug")
        restored = _round_trip(original)
        assert restored.task_id == original.task_id
        assert restored.session_id == original.session_id
        assert restored.task == original.task
        assert restored.correlation_id == original.correlation_id

    def test_uc_ag_02_agent_status_working(self):
        """UC-AG-02: Frontier loop signals 'working' — TUI shows spinner."""
        e = AgentStatus(status="working", node="frontier_loop", task="Refactor auth")
        assert e.status == "working"
        assert e.task == "Refactor auth"
        assert e.turns is None

    def test_uc_ag_02_agent_status_idle(self):
        """UC-AG-02: Frontier loop signals 'idle' — TUI hides spinner."""
        e = AgentStatus(status="idle", node="frontier_loop", turns=5, tool_calls=12)
        assert e.status == "idle"
        assert e.turns == 5
        assert e.tool_calls == 12
        assert e.task is None

    def test_uc_ag_03_agent_end_success(self):
        """UC-AG-03: Agent task ends successfully."""
        e = AgentEnd(task_id="t1", session_id="s1", status="completed", result="Done")
        assert e.status == "completed"
        assert e.result == "Done"
        assert e.error is None

    def test_uc_ag_03_agent_end_failure(self):
        """UC-AG-03: Agent task ends with failure — error field populated."""
        e = AgentEnd(task_id="t1", session_id="s1", status="failed", error="OOM")
        assert e.status == "failed"
        assert e.error == "OOM"
        assert e.result is None

    def test_uc_ag_04_mode_changed(self):
        """UC-AG-04: Execution mode changes from plan to act."""
        e = AgentModeChanged(mode="act", tool="bash")
        assert e.mode == "act"
        assert e.tool == "bash"

    def test_uc_ag_05_plan_committed(self):
        """UC-AG-05: Agent commits a 4-step plan."""
        e = AgentPlanCommitted(step_count=4, tool="delegate")
        assert e.step_count == 4


# ---------------------------------------------------------------------------
# UC-TL — Tool execution
# ---------------------------------------------------------------------------

class TestToolExecution:
    """Use-cases: UC-TL-01 … UC-TL-07"""

    def test_uc_tl_01_tool_execute_start(self):
        """UC-TL-01: TUI creates a 'running' tool widget when this fires."""
        e = ToolExecuteStart(tool="read_file", args={"path": "main.py"}, tool_call_id="tc1")
        assert e.tool == "read_file"
        assert e.args["path"] == "main.py"
        assert e.tool_call_id == "tc1"

    def test_uc_tl_02_tool_invoked(self):
        """UC-TL-02: Invocation record persisted after tool dispatched."""
        e = ToolInvoked(
            session_update={}, tool_call_id="tc1", title="read_file",
            status="invoked", timestamp=time.time(), workdir="/tmp",
        )
        assert e.status == "invoked"
        assert e.workdir == "/tmp"

    def test_uc_tl_03_tool_execute_finish(self):
        """UC-TL-03: Tool widget updated to show result on success."""
        e = ToolExecuteFinish(
            session_update={}, tool_call_id="tc1", title="read_file",
            status="completed", content="file contents", raw_output="file contents",
            workdir="/tmp",
        )
        assert e.status == "completed"
        assert e.content == "file contents"

    def test_uc_tl_04_tool_execute_error(self):
        """UC-TL-04: Tool widget marked failed on error."""
        e = ToolExecuteError(
            session_update={}, tool_call_id="tc1", title="bash",
            status="failed", content=None, error="Permission denied", workdir="/tmp",
        )
        assert e.status == "failed"
        assert "Permission denied" in e.error

    def test_uc_tl_05_tool_result(self):
        """UC-TL-05: Raw tool result captured for audit log."""
        e = ToolResult(tool="grep", result={"matches": 3}, turn=2)
        assert e.tool == "grep"
        assert e.result["matches"] == 3
        assert e.turn == 2

    def test_uc_tl_06_tool_permission_required(self):
        """UC-TL-06: Approval modal shown when tool needs permission."""
        e = ToolPermissionRequired(
            tool_id="tc1", tool="bash", args={"cmd": "rm -rf /"},
        )
        assert e.tool == "bash"
        assert e.tool_id == "tc1"
        assert "rm -rf" in e.args["cmd"]

    def test_uc_tl_07_doom_loop_detected(self):
        """UC-TL-07: Doom-loop triggers warning; behavior='ask'."""
        e = ToolDoomLoopDetected(tool="bash", fingerprint="abc123", behavior="ask")
        assert e.behavior == "ask"
        assert e.fingerprint == "abc123"


# ---------------------------------------------------------------------------
# UC-PM — Permission / approval
# ---------------------------------------------------------------------------

class TestPermissionApproval:
    """Use-cases: UC-PM-01 … UC-PM-03"""

    def test_uc_pm_01_spawn_permission_required(self):
        """UC-PM-01: Spawn modal shown before delegating to sub-agent."""
        e = SpawnPermissionRequired(
            tool="delegate", role="analyst",
            task="Analyse authentication", tool_id="sp1",
        )
        assert e.role == "analyst"
        assert e.tool_id == "sp1"

    def test_uc_pm_02_bash_approved(self):
        """UC-PM-02: User approves bash command — execution resumes."""
        e = BashApprovalGranted(tool_id="tc1")
        assert e.tool_id == "tc1"
        _has_base_fields(e)

    def test_uc_pm_03_bash_denied(self):
        """UC-PM-03: User denies bash command — tool cancelled."""
        e = BashApprovalDenied(tool_id="tc1")
        assert e.tool_id == "tc1"


# ---------------------------------------------------------------------------
# UC-PV — Preview / plan
# ---------------------------------------------------------------------------

class TestPreviewPlan:
    """Use-cases: UC-PV-01 … UC-PV-05"""

    def test_uc_pv_01_preview_pending(self):
        """UC-PV-01: Preview panel shown when plan is ready for review."""
        e = PreviewPending(preview_id="plan")
        assert e.preview_id == "plan"

    def test_uc_pv_02_preview_confirmed(self):
        """UC-PV-02: Blocked tool resumes after user confirms."""
        e = PreviewConfirmed(preview_id="plan")
        assert e.preview_id == "plan"

    def test_uc_pv_03_preview_rejected(self):
        """UC-PV-03: Tool cancelled after user rejects preview."""
        e = PreviewRejected(preview_id="plan")
        assert e.preview_id == "plan"

    def test_uc_pv_04_plan_requested(self):
        """UC-PV-04: Render plan approval dialog before execution."""
        e = PlanRequested(plan=[{"step": 1}], blocked_tool="bash", session_id="s1")
        assert e.blocked_tool == "bash"
        assert len(e.plan) == 1

    def test_uc_pv_05_plan_progress(self):
        """UC-PV-05: Progress bar updates as each plan step completes."""
        e = PlanProgress(plan_progress={"done": 2, "total": 5})
        assert e.plan_progress["done"] == 2


# ---------------------------------------------------------------------------
# UC-ST — Step (graph node progress)
# ---------------------------------------------------------------------------

class TestSteps:
    """Use-cases: UC-ST-01, UC-ST-02"""

    def test_uc_st_01_step_start(self):
        """UC-ST-01: TUI footer shows 'Step 1 of 3' when step begins."""
        e = StepStart(step=1, total=3, tool="bash", description="Run tests", session_id="s1")
        assert e.step == 1
        assert e.total == 3

    def test_uc_st_02_step_finish(self):
        """UC-ST-02: Step marked done; elapsed time shown."""
        e = StepFinish(
            step=1, total=3, tool="bash", ok=True,
            elapsed_ms=123.4, tool_call_count=2, session_id="s1",
        )
        assert e.ok is True
        assert e.elapsed_ms == 123.4
        assert e.tool_call_count == 2

    def test_uc_st_02_step_finish_failed(self):
        """UC-ST-02: Step marked failed when tool returns error."""
        e = StepFinish(
            step=2, total=3, tool="bash", ok=False,
            elapsed_ms=50.0, tool_call_count=1, session_id="s1",
        )
        assert e.ok is False


# ---------------------------------------------------------------------------
# UC-SE — Session
# ---------------------------------------------------------------------------

class TestSession:
    """Use-cases: UC-SE-01 … UC-SE-05"""

    def test_uc_se_01_session_created_from_server(self):
        """UC-SE-01 (server): Session tab added to TUI on creation."""
        e = SessionCreated(session_id="s1", metadata={"label": "dev"})
        assert e.session_id == "s1"
        assert e.task_id is None

    def test_uc_se_01_session_created_from_task(self):
        """UC-SE-01 (task): Session associated with a task."""
        e = SessionCreated(session_id="s1", task_id="t1")
        assert e.task_id == "t1"

    def test_uc_se_02_session_new(self):
        """UC-SE-02: Blank session resets chat view."""
        ts = time.time()
        e = SessionNew(timestamp=ts)
        assert e.timestamp == ts

    def test_uc_se_03_session_hydrated(self):
        """UC-SE-03: Chat history restored from storage."""
        history = [{"role": "user", "content": "hello"}]
        e = SessionHydrated(
            session_id="s1", message_history=history,
            current_task="fix bug", working_dir="/repo",
        )
        assert len(e.message_history) == 1
        assert e.working_dir == "/repo"

    def test_uc_se_03_session_hydrated_no_task(self):
        """UC-SE-03: Session hydrated without an active task."""
        e = SessionHydrated(
            session_id="s1", message_history=[],
            current_task=None, working_dir="/repo",
        )
        assert e.current_task is None

    def test_uc_se_04_session_title_generated(self):
        """UC-SE-04: Session tab label updated to auto-generated title."""
        e = SessionTitleGenerated(title="Refactor login module")
        assert e.title == "Refactor login module"

    def test_uc_se_05_session_files_changed(self):
        """UC-SE-05: File tree refreshed when workspace changes."""
        e = SessionFilesChanged(
            files=[{"path": "src/auth.py", "absolute": "/repo/src/auth.py"}],
            workdir="/repo", is_git_repo=True,
        )
        assert len(e.files) == 1
        assert e.is_git_repo is True


# ---------------------------------------------------------------------------
# UC-PR — Provider / model
# ---------------------------------------------------------------------------

class TestProviderModel:
    """Use-cases: UC-PR-01 … UC-PR-11"""

    def test_uc_pr_01_provider_connected(self):
        """UC-PR-01: Provider indicator turns green on connect."""
        e = ProviderStatusChanged(provider="anthropic", status="connected")
        assert e.status == "connected"

    def test_uc_pr_01_provider_disconnected(self):
        """UC-PR-01: Provider indicator turns red on disconnect."""
        e = ProviderStatusChanged(provider="anthropic", status="disconnected")
        assert e.status == "disconnected"

    def test_uc_pr_02_models_list(self):
        """UC-PR-02: Model dropdown populated from fresh list."""
        e = ProviderModelsList(provider="anthropic", models=["claude-3-5-sonnet", "claude-3-haiku"])
        assert "claude-3-5-sonnet" in e.models

    def test_uc_pr_03_models_cached(self):
        """UC-PR-03: Model dropdown populated from cache (stale indicator)."""
        e = ProviderModelsCached(provider="anthropic", models=["claude-3-5-sonnet"])
        assert e.provider == "anthropic"

    def test_uc_pr_03_models_cached_no_list(self):
        """UC-PR-03: Cached event may carry no model list (probe shortcut)."""
        e = ProviderModelsCached(provider="lm_studio")
        assert e.models is None

    def test_uc_pr_04_models_empty(self):
        """UC-PR-04: 'No models available' warning shown."""
        e = ProviderModelsEmpty(provider="ollama")
        assert e.provider == "ollama"

    def test_uc_pr_05_models_updated(self):
        """UC-PR-05: Model selector refreshed after settings fetch."""
        e = ProviderModelsUpdated(provider="openai", models=["gpt-4o", "gpt-4o-mini"])
        assert "gpt-4o" in e.models

    def test_uc_pr_06_selection_changed(self):
        """UC-PR-06: Active provider/model updated after user choice."""
        e = ProviderSelectionChanged(provider="openai", model="gpt-4o")
        assert e.model == "gpt-4o"

    def test_uc_pr_07_context_window(self):
        """UC-PR-07: Token budget calibrated to provider context window."""
        e = ProviderContextWindow(provider="anthropic", model="claude-3-5-sonnet", context_window=200000)
        assert e.context_window == 200000

    def test_uc_pr_08_provider_unavailable(self):
        """UC-PR-08: Fatal startup error shown when no provider available."""
        e = ProviderUnavailable(reason="API key not set")
        assert "API key" in e.reason

    def test_uc_pr_09_config_missing(self):
        """UC-PR-09: Banner with actionable path shown for missing config."""
        e = ProviderConfigMissing(path="~/.config/providers.json")
        assert "providers.json" in e.path

    def test_uc_pr_10_model_missing(self):
        """UC-PR-10: Warning shown when configured model not available; fallback offered."""
        e = ProviderModelMissing(
            provider="anthropic", requested="claude-2",
            available=["claude-3-5-sonnet"],
        )
        assert e.requested == "claude-2"
        assert len(e.available) == 1

    def test_uc_pr_11_provider_limit(self):
        """UC-PR-11: Rate-limit warning shown; execution backs off."""
        e = ProviderLimit(error="429 Too Many Requests")
        assert "429" in e.error


# ---------------------------------------------------------------------------
# UC-IN — Inference / streaming
# ---------------------------------------------------------------------------

class TestInference:
    """Use-cases: UC-IN-01 … UC-IN-08"""

    def test_uc_in_01_stream_chunk(self):
        """UC-IN-01: Streaming text appended to chat widget."""
        e = ResponseStreamChunk(chunk="Hello ", is_reasoning=False)
        assert e.chunk == "Hello "
        assert e.is_reasoning is False

    def test_uc_in_01_stream_chunk_reasoning(self):
        """UC-IN-01: Reasoning chunk displayed in separate reasoning box."""
        e = ResponseStreamChunk(chunk="<think>", is_reasoning=True)
        assert e.is_reasoning is True

    def test_uc_in_02_stream_end(self):
        """UC-IN-02: Copy button enabled after stream complete."""
        e = ResponseStreamEnd(full_text="Hello world")
        assert e.full_text == "Hello world"

    def test_uc_in_03_model_token_partial(self):
        """UC-IN-03: Partial token appended to streaming display."""
        e = ModelToken(text="Hel", partial=True)
        assert e.partial is True
        assert e.full is None

    def test_uc_in_04_model_token_final(self):
        """UC-IN-04: Final token received; counter incremented."""
        e = ModelToken(text="", partial=False, full="Hello world")
        assert e.partial is False
        assert e.full == "Hello world"

    def test_uc_in_05_llm_token_with_reasoning_flag(self):
        """UC-IN-05: Reasoning tokens routed to separate display pane."""
        e = LLMToken(text="let me think", partial=True, is_reasoning=True)
        assert e.is_reasoning is True

    def test_uc_in_06_model_response_telemetry(self):
        """UC-IN-06: Cost estimation uses token counts from telemetry."""
        e = ModelResponse(
            provider="anthropic", model="claude-3-5-sonnet",
            prompt_tokens=1000, completion_tokens=200, total_tokens=1200,
            latency=1.5, ts=time.time(),
        )
        assert e.total_tokens == 1200
        assert e.latency == 1.5
        assert e.extra is None

    def test_uc_in_07_model_routing(self):
        """UC-IN-07: Active model displayed in status bar."""
        e = ModelRouting(
            selected="claude-3-5-sonnet", provider="anthropic",
            available_models=["claude-3-5-sonnet", "claude-3-haiku"],
        )
        assert e.selected == "claude-3-5-sonnet"

    def test_uc_in_08_routing_complete(self):
        """UC-IN-08: Settings UI confirms provider switch."""
        e = ModelRoutingComplete(model="gpt-4o", provider="openai", switched_provider=True)
        assert e.switched_provider is True


# ---------------------------------------------------------------------------
# UC-CM — Context / memory
# ---------------------------------------------------------------------------

class TestContextMemory:
    """Use-cases: UC-CM-01 … UC-CM-06"""

    def test_uc_cm_01_context_overflow(self):
        """UC-CM-01: Compaction triggered when prompt exceeds context window."""
        e = ContextOverflow(
            prompt_tokens=198000, budget=200000,
            reserved=2000, session_id="s1", source="pre_flight",
        )
        assert e.prompt_tokens >= e.budget - e.reserved

    def test_uc_cm_01_context_overflow_api_error(self):
        """UC-CM-01: Overflow detected from API error (post-call path)."""
        e = ContextOverflow(
            prompt_tokens=0, budget=0,
            reserved=0, session_id="s1", source="api_error",
        )
        assert e.source == "api_error"

    def test_uc_cm_02_context_compacted(self):
        """UC-CM-02: Confirmation shown after manual compaction."""
        e = ContextCompacted(
            message="Context compacted", method="summary",
            tokens_before=100000, tokens_after=20000,
        )
        assert e.tokens_after < e.tokens_before

    def test_uc_cm_03_auto_compacted(self):
        """UC-CM-03: Token counter updated silently after auto-compaction."""
        e = ContextAutoCompacted(
            method="summary", tokens_before=190000,
            tokens_after=30000, new_message_count=5, session_id="s1",
        )
        assert e.new_message_count == 5

    def test_uc_cm_04_compact_failed(self):
        """UC-CM-04: Error banner shown when compaction fails."""
        e = ContextCompactFailed(message="LLM summarisation failed")
        assert "summarisation" in e.message

    def test_uc_cm_05_message_truncation(self):
        """UC-CM-05: Chat shows 'X messages dropped' notice."""
        e = MessageTruncation(dropped_count=10, dropped_tokens=5000, tokens_after=80000)
        assert e.dropped_count == 10
        assert e.tokens_after == 80000

    def test_uc_cm_06_compaction_applied(self):
        """UC-CM-06: Token-savings dashboard updated after scheduler compaction."""
        e = MessageCompactionApplied(
            source="scheduler", original_count=200, new_count=40,
            dropped_count=160, original_tokens=80000, new_tokens=12000,
            tokens_reduced=68000,
        )
        assert e.tokens_reduced == 68000
        assert e.dropped_count == e.original_count - e.new_count


# ---------------------------------------------------------------------------
# UC-TB — Token budget / usage
# ---------------------------------------------------------------------------

class TestTokenBudget:
    """Use-cases: UC-TB-01 … UC-TB-04"""

    def test_uc_tb_01_budget_display(self):
        """UC-TB-01: Footer shows 'used 75% of budget' warning."""
        e = TokenBudget(used=150000, limit=200000, percent=75.0, warning=True)
        assert e.warning is True
        assert e.percent == 75.0

    def test_uc_tb_02_budget_raw_update(self):
        """UC-TB-02: Compaction check triggered when usage_ratio > 0.9."""
        e = TokenBudgetUpdate(
            used_tokens=180000, max_tokens=200000,
            usage_ratio=0.9, session_id="s1",
        )
        assert e.usage_ratio == 0.9

    def test_uc_tb_03_turn_summary(self):
        """UC-TB-03: Running cost display updated after each LLM turn."""
        e = UsageTurnSummary(
            prompt_tokens=1000, completion_tokens=200, total_tokens=1200,
            cache_creation_tokens=0, cache_read_tokens=800,
            cost_usd=0.006, session_cost_usd=0.018,
            model="claude-3-5-sonnet", task_id="t1",
        )
        assert e.total_tokens == e.prompt_tokens + e.completion_tokens
        assert e.session_cost_usd > e.cost_usd

    def test_uc_tb_04_budget_exceeded(self):
        """UC-TB-04: Execution stops when session cost exceeds ceiling."""
        e = UsageBudgetExceeded(session_cost_usd=5.50, budget_ceiling_usd=5.00)
        assert e.session_cost_usd > e.budget_ceiling_usd


# ---------------------------------------------------------------------------
# UC-FS — File system
# ---------------------------------------------------------------------------

class TestFileSystem:
    """Use-cases: UC-FS-01, UC-FS-02"""

    def test_uc_fs_01_file_modified(self):
        """UC-FS-01: File tree highlights file modified by tool."""
        e = FileModified(path="src/auth.py", tool="write_file", workdir="/repo")
        assert e.path == "src/auth.py"
        assert e.tool == "write_file"

    def test_uc_fs_02_file_deleted(self):
        """UC-FS-02: File removed from file tree after deletion."""
        e = FileDeleted(path="tmp/cache.db", workdir="/repo")
        assert e.path == "tmp/cache.db"


# ---------------------------------------------------------------------------
# UC-DL — Delegation
# ---------------------------------------------------------------------------

class TestDelegation:
    """Use-cases: UC-DL-01 … UC-DL-04"""

    def test_uc_dl_01_delegation_complete(self):
        """UC-DL-01: Parent agent resumes after all sub-agents finish."""
        e = DelegationComplete(count=3, keys=["k1", "k2", "k3"], session_id="s1")
        assert e.count == len(e.keys)

    def test_uc_dl_02_scout_files_discovered(self):
        """UC-DL-02: Discovered files fed into next analysis step."""
        e = AgentScoutFilesDiscovered(
            files=["src/auth.py", "src/login.py"],
            agent_id="scout-1", result={"summary": "found 2 files"},
        )
        assert len(e.files) == 2

    def test_uc_dl_03_researcher_doc_summary(self):
        """UC-DL-03: Doc summary accumulated for synthesis."""
        e = AgentResearcherDocSummary(
            summary="Auth module uses JWT", agent_id="researcher-1",
        )
        assert "JWT" in e.summary

    def test_uc_dl_04_reviewer_bug_found(self):
        """UC-DL-04: Bug list aggregated for review report."""
        e = AgentReviewerBugFound(
            bugs=[{"severity": "high", "description": "SQL injection"}],
            agent_id="reviewer-1", result={"status": "bugs_found"},
        )
        assert e.bugs[0]["severity"] == "high"

    def test_uc_dl_04_reviewer_no_bugs(self):
        """UC-DL-04: Empty bug list signals clean review."""
        e = AgentReviewerBugFound(bugs=[], agent_id="reviewer-1", result={"status": "clean"})
        assert len(e.bugs) == 0


# ---------------------------------------------------------------------------
# UC-SC — Scheduler
# ---------------------------------------------------------------------------

class TestScheduler:
    """Use-cases: UC-SC-01, UC-SC-02"""

    def test_uc_sc_01_distill_request(self):
        """UC-SC-01: Background distillation worker triggered."""
        ts = time.time()
        e = SchedulerDistillRequest(source="scheduler", time=ts)
        assert e.source == "scheduler"
        assert e.time == ts

    def test_uc_sc_02_distill_completed(self):
        """UC-SC-02: Scheduler notified that memory is fresh."""
        e = SchedulerDistillCompleted(source="scheduler")
        assert e.source == "scheduler"


# ---------------------------------------------------------------------------
# UC-MC — MCP
# ---------------------------------------------------------------------------

class TestMcp:
    """Use-cases: UC-MC-01, UC-MC-02"""

    def test_uc_mc_01_server_running(self):
        """UC-MC-01: MCP indicator turns green when server starts."""
        e = McpServerStatus(running=True, count=1, server_names=["codingagent"])
        assert e.running is True
        assert "codingagent" in e.server_names

    def test_uc_mc_01_server_stopped(self):
        """UC-MC-01: MCP indicator turns grey when server stops."""
        e = McpServerStatus(running=False, count=0)
        assert e.running is False
        assert e.count == 0
        assert e.server_names == []

    def test_uc_mc_01_server_status_with_rich_servers_dict(self):
        """UC-MC-01: manager.py passes full server dict in 'servers' field."""
        servers = {"codingagent": {"status": "connected", "tool_count": 5}}
        e = McpServerStatus(running=True, count=1, servers=servers)
        assert e.servers["codingagent"]["tool_count"] == 5

    def test_uc_mc_02_tools_list_changed(self):
        """UC-MC-02: Tool palette refreshed after MCP tool list updates."""
        e = McpToolsListChanged(server="codingagent", params={"added": ["new_tool"]})
        assert e.server == "codingagent"


# ---------------------------------------------------------------------------
# UC-CF — Config
# ---------------------------------------------------------------------------

class TestConfig:
    """Use-case: UC-CF-01"""

    def test_uc_cf_01_config_reloaded(self):
        """UC-CF-01: Settings applied without restart after hot-reload."""
        e = ConfigReloaded(changed_paths=["config/settings.json"])
        assert "settings.json" in e.changed_paths[0]

    def test_uc_cf_01_multiple_paths(self):
        """UC-CF-01: Multiple changed files reported in a single event."""
        e = ConfigReloaded(changed_paths=["config/a.json", "config/b.json"])
        assert len(e.changed_paths) == 2


# ---------------------------------------------------------------------------
# UC-OR — Orchestrator lifecycle
# ---------------------------------------------------------------------------

class TestOrchestratorLifecycle:
    """Use-cases: UC-OR-01 … UC-OR-04"""

    def test_uc_or_01_startup(self):
        """UC-OR-01: Input field enabled; 'ready' shown after startup."""
        ts = time.time()
        e = OrchestratorStartup(time=ts, working_dir="/repo")
        assert e.working_dir == "/repo"

    def test_uc_or_02_models_check_started(self):
        """UC-OR-02: Loading indicator shown in model selector."""
        e = OrchestratorModelsCheckStarted(payload={"provider": "anthropic"})
        assert e.payload["provider"] == "anthropic"

    def test_uc_or_03_models_check_completed(self):
        """UC-OR-03: Loading indicator hidden; model selector enabled."""
        e = OrchestratorModelsCheckCompleted(payload={"models": ["claude-3-5-sonnet"]})
        assert len(e.payload["models"]) == 1

    def test_uc_or_04_models_check_failed(self):
        """UC-OR-04: Error shown; user prompted to check provider settings."""
        e = OrchestratorModelsCheckFailed(payload={"error": "Connection refused"})
        assert "Connection refused" in e.payload["error"]


# ---------------------------------------------------------------------------
# UC-UI — UI / notifications
# ---------------------------------------------------------------------------

class TestUiNotifications:
    """Use-cases: UC-UI-01 … UC-UI-04"""

    def test_uc_ui_01_warning_notification(self):
        """UC-UI-01: Warning banner shown in TUI."""
        e = UiNotification(level="warning", message="Rate limit approaching")
        assert e.level == "warning"

    def test_uc_ui_01_error_notification(self):
        """UC-UI-01: Error toast shown in TUI."""
        e = UiNotification(level="error", message="API key invalid", source="auth_check")
        assert e.level == "error"
        assert e.source == "auth_check"

    def test_uc_ui_02_hook_message(self):
        """UC-UI-02: Hook output displayed in tool area."""
        e = HookMessage(tool_name="bash", event="post_run", message="Tests passed")
        assert e.message == "Tests passed"

    def test_uc_ui_03_git_branch(self):
        """UC-UI-03: Branch name and dirty indicator shown in TUI footer."""
        e = GitBranch(branch="main", dirty=True, ahead=2, behind=0)
        assert e.dirty is True
        assert e.ahead == 2

    def test_uc_ui_04_working_dir_unavailable(self):
        """UC-UI-04: Error blocks input when working dir is inaccessible."""
        e = WorkingDirUnavailable(path="/missing/repo", error="No such file or directory")
        assert "No such file" in e.error


# ---------------------------------------------------------------------------
# UC-PC — Perception
# ---------------------------------------------------------------------------

class TestPerception:
    """Use-cases: UC-PC-01, UC-PC-02"""

    def test_uc_pc_01_corrective_prompt(self):
        """UC-PC-01: Debug log records corrective injection frequency."""
        e = PerceptionCorrectivePrompt(
            session_id="s1", attempt=2,
            reason="no_tool", model_tier="sonnet",
            truncated_yaml="tools: []",
        )
        assert e.reason == "no_tool"
        assert e.attempt == 2

    def test_uc_pc_02_turn_limit_reached(self):
        """UC-PC-02: Execution stops and notice shown when turn limit hit."""
        e = TaskTurnLimit(turn_count=50, max_turns=50)
        assert e.turn_count == e.max_turns


# ---------------------------------------------------------------------------
# UC-SD — Subagent dispatch
# ---------------------------------------------------------------------------

class TestSubagentDispatch:
    """Use-cases: UC-SD-01, UC-SD-02"""

    def test_uc_sd_01_dispatch(self):
        """UC-SD-01: Delegation chain tracking records dispatched sub-agent."""
        e = SubagentDispatch(delegation_key="dk1", role="analyst", task="Analyse auth")
        assert e.delegation_key == "dk1"
        assert e.role == "analyst"

    def test_uc_sd_02_result_returned(self):
        """UC-SD-02: Parent agent receives sub-agent result and resumes."""
        e = SubagentResult(
            delegation_key="dk1", role="analyst",
            task="Analyse auth", result={"findings": "JWT used"},
        )
        assert e.result["findings"] == "JWT used"


# ---------------------------------------------------------------------------
# UC-SER — Serialisation (cross-cutting)
# ---------------------------------------------------------------------------

class TestSerialisation:
    """Use-cases: UC-SER-01, UC-SER-02 — all events must round-trip and carry base fields."""

    ALL_SAMPLE_EVENTS = [
        AgentStart(task_id="t1", session_id="s1", task="test"),
        AgentStatus(status="idle", node="frontier_loop", turns=1, tool_calls=0),
        AgentEnd(task_id="t1", session_id="s1", status="completed"),
        AgentModeChanged(mode="act", tool="bash"),
        AgentPlanCommitted(step_count=3, tool="delegate"),
        AgentMessage(message="hello", attachments=None, status="info"),
        AgentWaitingForUser(question="Continue?"),
        ToolExecuteStart(tool="bash", args={}, tool_call_id="tc1"),
        ToolResult(tool="grep", result={}, turn=1),
        ToolPermissionRequired(tool_id="tc1", tool="bash", args={}),
        ToolDoomLoopDetected(tool="bash", fingerprint="fp1", behavior="ask"),
        SpawnPermissionRequired(tool="delegate", role="analyst", task="t", tool_id="sp1"),
        BashApprovalGranted(tool_id="tc1"),
        BashApprovalDenied(tool_id="tc1"),
        PreviewPending(preview_id="plan"),
        PreviewConfirmed(preview_id="plan"),
        PreviewRejected(preview_id="plan"),
        StepStart(step=1, total=2, tool="bash", description="Run", session_id="s1"),
        StepFinish(step=1, total=2, tool="bash", ok=True, elapsed_ms=10.0, tool_call_count=1, session_id="s1"),
        SessionCreated(session_id="s1"),
        SessionTitleGenerated(title="Test session"),
        SessionRegistered(session_id="s1"),
        SessionUnregistered(session_id="s1"),
        SessionHealthAlert(session_id="s1", message="stale"),
        SessionRequestState(session_id="s1"),
        ProviderStatusChanged(provider="anthropic", status="connected"),
        ProviderModelsList(provider="anthropic", models=["m1"]),
        ProviderModelsEmpty(provider="ollama"),
        ProviderUnavailable(reason="No key"),
        ProviderConfigMissing(path="/tmp/config.json"),
        ProviderLimit(error="429"),
        ResponseStreamChunk(chunk="hi", is_reasoning=False),
        ResponseStreamEnd(full_text="hello"),
        ModelToken(text="hi", partial=True),
        LLMToken(text="hi", partial=False, is_reasoning=False),
        ContextOverflow(prompt_tokens=0, budget=0, reserved=0, session_id="s1", source="api_error"),
        ContextCompactFailed(message="failed"),
        ContextDegraded(session_id="s1", reason="truncation"),
        MessageTruncation(dropped_count=5, dropped_tokens=1000, tokens_after=5000),
        TokenBudget(used=100, limit=200, percent=50.0, warning=False),
        TokenBudgetWarning(used=180, limit=200, percent=90.0),
        UsageBudgetExceeded(session_cost_usd=6.0, budget_ceiling_usd=5.0),
        UsageSubagentCost(child_session_id="cs1", role="analyst", cost_usd=0.05),
        FileModified(path="a.py", tool="write_file", workdir="/repo"),
        FileDeleted(path="b.py", workdir="/repo"),
        FileDiffPreview(path="a.py", diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new"),
        DelegationStart(child_session_id="cs1", parent_session_id="ps1", role="analyst", task="review"),
        DelegationFinish(child_session_id="cs1", role="analyst", ok=True, cost_usd=0.1),
        DelegationComplete(count=1, keys=["k1"], session_id="s1"),
        SchedulerDistillRequest(source="scheduler", time=1.0),
        SchedulerDistillCompleted(source="scheduler"),
        McpServerStatus(running=True, count=1),
        McpToolsListChanged(server="mcp", params={}),
        ConfigReloaded(changed_paths=["c.json"]),
        SystemSettings(
            active_mode="auto", theme="dark", context_window=128000,
            default_provider="anthropic", default_model="sonnet",
            providers=[], autonomous_mode=True, max_turns=50,
        ),
        OrchestratorStartup(time=1.0, working_dir="/repo"),
        OrchestratorModelsCheckStarted(payload={}),
        OrchestratorModelsCheckCompleted(payload={}),
        OrchestratorModelsCheckFailed(payload={}),
        UiNotification(level="info", message="ok"),
        HookMessage(tool_name="bash", event="post_run", message="ok"),
        LogEntry(level="INFO", message="hello", timestamp=1.0),
        GitBranch(branch="main", dirty=False, ahead=0, behind=0),
        WorkingDirUnavailable(path="/x", error="not found"),
        RoleTransition(role="coding"),
        RetryAttempt(attempt=1, max_attempts=3, reason="timeout"),
        RetrySucceeded(attempt=2, reason="recovered"),
        RetryFailed(attempt=3, max_attempts=3, reason="exhausted"),
        TaskQueueUpdated(pending_tasks=2, queue_size=5, session_id="s1"),
        PerceptionCorrectivePrompt(session_id="s1", attempt=1, reason="no_tool", model_tier="sonnet", truncated_yaml=""),
        TaskTurnLimit(turn_count=10, max_turns=10),
        SubagentDispatch(delegation_key="dk1", role="analyst", task="t"),
        SubagentResult(delegation_key="dk1", role="analyst", task="t", result={}),
    ]

    @pytest.mark.parametrize("event", ALL_SAMPLE_EVENTS, ids=lambda e: type(e).__name__)
    def test_uc_ser_01_round_trip(self, event: Event):
        """UC-SER-01: Every event survives to_dict() → from_dict() round-trip."""
        restored = _round_trip(event)
        assert type(restored) is type(event)
        assert restored.correlation_id == event.correlation_id
        assert restored.timestamp == event.timestamp

    @pytest.mark.parametrize("event", ALL_SAMPLE_EVENTS, ids=lambda e: type(e).__name__)
    def test_uc_ser_02_base_fields_present(self, event: Event):
        """UC-SER-02: Every event carries a non-empty correlation_id and positive timestamp."""
        _has_base_fields(event)
