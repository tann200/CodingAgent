"""BridgeSubscriptionsMixin — EventBus subscription setup and teardown.

Contains: setup_subscriptions, _seed_context_window_from_config, cleanup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Type

if TYPE_CHECKING:
    from src.core.messaging import Event, EventHandler

from ._bridge_protocol import AgentBridgeProtocol
from .logging import get_logger

# ── Typed event routing table ────────────────────────────────────────────────
# Maps each typed event class to the handler method that should receive it.
# The handler receives a dict payload converted from the typed event via
# event.to_dict().  Events with lambda no-op handlers in the old subscription
# block are deliberately omitted.
# ---------------------------------------------------------------------------

try:
    from src.core.messaging import (
        AgentMessage,
        # Agent lifecycle
        ContextCompacted,
        ContextDegraded,
        DelegationFinish,
        DelegationStart,
        FileDeleted,
        FileDiffPreview,
        FileModified,
        GitBranch,
        LogEntry,
        McpServerStatus,
        ModelResponse,
        ModelRouting,
        ModelToken,
        # Context / memory
        # Delegation
        # File system
        # UI / notifications
        # Retry
        # Tool execution
        # Session
        # Token budget
        # Step
        # Provider / model
        # Preview / plan
        OrchestratorStartup,
        PlanProgress,
        PlanRequested,
        PreviewPending,
        ProviderContextWindow,
        ProviderModelsCached,
        ProviderModelsList,
        ProviderStatusChanged,
        ProviderUnavailable,
        ResponseStreamChunk,
        RetryAttempt,
        RetryFailed,
        RetrySucceeded,
        RoleTransition,
        SessionHealthAlert,
        SessionHydrated,
        SessionNew,
        SpawnPermissionRequired,
        StepFinish,
        StepStart,
        SystemSettings,
        TaskQueueUpdated,
        TokenBudget,
        TokenBudgetUpdate,
        TokenBudgetWarning,
        ToolDoomLoopDetected,
        ToolExecuteError,
        ToolExecuteFinish,
        ToolExecuteStart,
        ToolPermissionRequired,
        UiNotification,
        UsageSubagentCost,
        UsageTurnSummary,
    )
except Exception:
    # Mock / dev mode — ignore
    pass


# (event_cls, handler_method_name)
# Order matches the old setup_subscriptions() block for maintainability.
TYPED_EVENT_ROUTING: list[tuple[type, str]] = [
    (SystemSettings, "_on_system_settings"),
    (OrchestratorStartup, "_on_orchestrator_startup"),
    (ProviderStatusChanged, "_on_provider_status"),
    (ProviderUnavailable, "_on_provider_unavailable"),
    (ProviderModelsList, "_on_models_list"),
    (ProviderModelsCached, "_on_models_list"),
    (ModelRouting, "_on_model_routing"),
    (ModelResponse, "_on_model_response"),
    (ModelToken, "_on_model_token"),
    (ResponseStreamChunk, "_on_stream_chunk"),
    (ToolExecuteStart, "_on_tool_start"),
    (ToolExecuteFinish, "_on_tool_finish"),
    (ToolExecuteError, "_on_tool_error"),
    (FileDiffPreview, "_on_diff_preview"),
    (FileModified, "_on_file_modified"),
    (FileDeleted, "_on_file_deleted"),
    (PlanProgress, "_on_plan_progress"),
    (PlanRequested, "_on_plan_requested"),
    (SessionNew, "_on_session_new"),
    (SessionHydrated, "_on_session_hydrated"),
    (SessionHealthAlert, "_on_session_health"),
    (UiNotification, "_on_ui_notification"),
    (LogEntry, "_on_log_new"),
    (TokenBudgetUpdate, "_on_token_budget"),
    (TokenBudgetWarning, "_on_token_budget_warning"),
    (TokenBudget, "_on_token_budget"),
    (ProviderContextWindow, "_on_provider_context_window"),
    (RoleTransition, "_on_role_transition"),
    (PreviewPending, "_on_preview_pending"),
    (GitBranch, "_on_git_branch"),
    (RetryAttempt, "_on_retry_attempt"),
    (RetrySucceeded, "_on_retry_succeeded"),
    (RetryFailed, "_on_retry_failed"),
    (ContextDegraded, "_on_context_degraded"),
    (ContextCompacted, "_on_context_compacted"),
    (TaskQueueUpdated, "_on_task_queue_updated"),
    (StepStart, "_on_step_start"),
    (StepFinish, "_on_step_finish"),
    (McpServerStatus, "_on_mcp_server_status"),
    (ToolPermissionRequired, "_on_tool_permission_required"),
    (SpawnPermissionRequired, "_on_spawn_permission_required"),
    (UsageTurnSummary, "_on_usage_turn_summary"),
    (UsageSubagentCost, "_on_subagent_cost"),
    (ToolDoomLoopDetected, "_on_doom_loop_detected"),
    (AgentMessage, "_on_agent_message"),
    (DelegationStart, "_on_delegation_start"),
    (DelegationFinish, "_on_delegation_finish"),
]


logger = get_logger("bridge")


class BridgeSubscriptionsMixin(AgentBridgeProtocol):
    """Mixin providing EventBus subscription lifecycle methods."""

    def _make_typed_adapter(self, method_name: str) -> Any:
        """Return an object with ``.handle(event)`` that converts the typed
        event to a dict and calls the named ``_on_*`` handler on ``self``.

        This lets existing dict-based handlers receive typed events through
        the new MessageBus without any refactoring.
        """
        fn: Callable[[dict], None] = getattr(self, method_name)

        class _DictBridgeAdapter:
            def handle(self, event: "Event") -> None:
                fn(event.to_dict())

        return _DictBridgeAdapter()

    def _setup_typed_subscriptions(self) -> None:
        """Register all typed event subscriptions on the MessageBus."""
        imported = 0
        skipped = 0
        for event_cls, method_name in TYPED_EVENT_ROUTING:
            try:
                self._subscribe_typed(event_cls, self._make_typed_adapter(method_name))
                imported += 1
            except Exception:
                skipped += 1
        if imported:
            logger.info(
                "MessageBus: subscribed %d typed events (%d skipped)",
                imported, skipped,
            )

    def setup_subscriptions(self) -> None:
        """Subscribe to every event in §4.5."""
        self._subscribe("system.settings", self._on_system_settings)
        # provider / model
        self._subscribe("orchestrator.startup", self._on_orchestrator_startup)
        self._subscribe("provider.status.changed", self._on_provider_status)
        self._subscribe("provider.unavailable", self._on_provider_unavailable)
        self._subscribe("provider.models.list", self._on_models_list)
        self._subscribe("provider.models.cached", self._on_models_list)
        self._subscribe("provider.models.empty", lambda p: None)
        self._subscribe("provider.model.missing", lambda p: None)
        self._subscribe("model.routing", self._on_model_routing)
        self._subscribe("model.response", self._on_model_response)
        self._subscribe("model.token", self._on_model_token)
        # TUI-10: per-chunk reasoning routing
        self._subscribe("response.stream_chunk", self._on_stream_chunk)
        # tool
        self._subscribe("tool.execute.start", self._on_tool_start)
        self._subscribe("tool.invoked", lambda p: None)
        self._subscribe("tool.execute.finish", self._on_tool_finish)
        self._subscribe("tool.execute.error", self._on_tool_error)
        # file
        self._subscribe("file.diff.preview", self._on_diff_preview)
        self._subscribe("file.modified", self._on_file_modified)
        self._subscribe("file.deleted", self._on_file_deleted)
        # plan
        self._subscribe("plan.progress", self._on_plan_progress)
        self._subscribe("plan.requested", self._on_plan_requested)
        # session
        self._subscribe("session.new", self._on_session_new)
        self._subscribe("session.hydrated", self._on_session_hydrated)
        self._subscribe("session.registered", lambda p: None)
        self._subscribe("session.unregistered", lambda p: None)
        self._subscribe("session.health_alert", self._on_session_health)
        # notifications / logging
        self._subscribe("ui.notification", self._on_ui_notification)
        self._subscribe("log.new", self._on_log_new)
        # token budget
        self._subscribe("token.budget.update", self._on_token_budget)
        self._subscribe("token.budget.warning", self._on_token_budget_warning)
        # canonical token.budget event published by orchestrator TUI-09
        self._subscribe("token.budget", self._on_token_budget)
        # context window from provider (e.g. LM Studio loaded_context_length)
        self._subscribe("provider.context_window", self._on_provider_context_window)
        # role transition — real backend and mock both publish via bus
        self._subscribe("role.transition", self._on_role_transition)
        # preview mode
        self._subscribe("preview.pending", self._on_preview_pending)
        self._subscribe("preview.confirmed", lambda p: None)
        self._subscribe("preview.rejected", lambda p: None)
        # git status
        self._subscribe("git.branch", self._on_git_branch)
        # retry / resilience
        self._subscribe("retry.attempt", self._on_retry_attempt)
        self._subscribe("retry.succeeded", self._on_retry_succeeded)
        self._subscribe("retry.failed", self._on_retry_failed)
        # context
        self._subscribe("context.degraded", self._on_context_degraded)
        self._subscribe("context.compacted", self._on_context_compacted)
        # task queue
        self._subscribe("task.queue.updated", self._on_task_queue_updated)
        # step boundaries (opencode-style)
        self._subscribe("step.start", self._on_step_start)
        self._subscribe("step.finish", self._on_step_finish)
        # MCP server status
        self._subscribe("mcp.server.status", self._on_mcp_server_status)
        # tool permission gate
        self._subscribe("tool.permission_required", self._on_tool_permission_required)
        self._subscribe("spawn.permission_required", self._on_spawn_permission_required)
        # per-turn token/cost summary (TUI-T6)
        self._subscribe("usage.turn_summary", self._on_usage_turn_summary)
        # GAP-NEW-7: subagent cost rollup — accumulate child cost into session total
        self._subscribe("usage.subagent_cost", self._on_subagent_cost)
        # doom-loop detection (PERM-W3)
        self._subscribe("tool.doom_loop_detected", self._on_doom_loop_detected)
        # CP-15: proactive mid-turn messages from send_user_message tool
        self._subscribe("agent.message", self._on_agent_message)
        # SUBAGENT-VIS-3: subagent lifecycle visibility
        self._subscribe("delegation.start", self._on_delegation_start)
        self._subscribe("delegation.finish", self._on_delegation_finish)

        logger.info(f"EventBus: subscribed to {len(self._subscriptions)} events")

        # Register typed MessageBus subscriptions alongside old EventBus
        self._setup_typed_subscriptions()

        # RACE-FIX: Immediately seed the token monitor and sidebar with the
        # context length from providers.json so the TOKEN BUDGET max is correct
        # on first display, without waiting for the async provider.context_window
        # event that may fire before subscriptions are registered.
        self._seed_context_window_from_config()

    def _seed_context_window_from_config(self) -> None:
        """Seed the token budget monitor and sidebar with the providers.json
        context_length immediately after setup_subscriptions() completes.

        This avoids the race condition where ProviderManager.initialize() fires
        ``provider.context_window`` before setup_subscriptions() has registered
        the ``_on_provider_context_window`` handler, causing the event to be lost
        and the sidebar to fall back to the 32,768 default.
        """
        try:
            from src.core.inference.provider_context import (  # type: ignore[import]
                _load_active_context_length,
                set_active_context_length,
            )

            ctx = _load_active_context_length()
            if ctx and ctx > 0:
                # Persist to the provider_context module so all consumers see it
                set_active_context_length(ctx)

                # Seed the token monitor under the "default" session (used before
                # the first task starts) so get_budget("default").max_tokens is
                # correct when the first token.budget event arrives.
                try:
                    from src.core.orchestration.token_budget import (  # type: ignore[import]
                        get_token_budget_monitor,
                    )

                    get_token_budget_monitor().update(
                        session_id="default", used_tokens=0, max_tokens=ctx
                    )
                except Exception:
                    pass

                # Fire a synthetic UpdateSettings so the sidebar label updates
                # immediately without waiting for a token.budget event.
                try:
                    from tui.tui_src.ui.events import UpdateSettings

                    self._post(UpdateSettings(updates={"context_window": ctx}))
                except Exception:
                    pass
        except Exception:
            pass

    def cleanup(self) -> None:
        """Unsubscribe all handlers (§10.2 step 5) and release thread pool."""
        pool = getattr(self, "_thread_pool", None)
        if pool is not None:
            pool.shutdown(wait=False)
        # Unsubscribe old EventBus handlers
        for event, cb in self._subscriptions:
            try:
                self._bus.unsubscribe(event, cb)
            except Exception:
                pass
        self._subscriptions.clear()
        # Unsubscribe typed MessageBus handlers
        typed_bus = getattr(self, "_typed_bus", None)
        if typed_bus is not None:
            for event_cls, handler in self._typed_subscriptions:
                try:
                    typed_bus.unsubscribe(event_cls, handler)
                except Exception:
                    pass
        self._typed_subscriptions.clear()
        logger.info("EventBus: all subscriptions removed")
