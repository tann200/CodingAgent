"""BridgeSubscriptionsMixin — MessageBus subscription setup and teardown.

Phase 5: the old EventBus subscriptions have been removed.  All bridge
subscriptions now go through the typed MessageBus.  The DualPublishBus
adapter ensures that every ``EventBus.publish()`` call from the backend
still reaches these typed handlers.

Contains: setup_subscriptions, _seed_context_window_from_config, cleanup.

Phase 3.3: per-symbol lazy loading via importlib.  A single missing symbol
logs a warning but does not affect any other subscription.
"""

from __future__ import annotations

import importlib as _importlib
import logging as _logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.core.messaging import Event

from ._bridge_protocol import AgentBridgeProtocol
from .logging import get_logger


# ── Lazy symbol resolution ──────────────────────────────────────────────────
# Each event class is imported individually so a missing symbol does not
# silently break all subscriptions.  The module is imported once and cached.
# ----------------------------------------------------------------------------

_SYMBOL_MODULE_PATH = "src.core.messaging"
_SYMBOL_MODULE = None

_log = _logging.getLogger("bridge")


def _resolve_event_class(name: str):
    global _SYMBOL_MODULE
    if _SYMBOL_MODULE is None:
        try:
            _SYMBOL_MODULE = _importlib.import_module(_SYMBOL_MODULE_PATH)
        except Exception as exc:
            _log.warning("bridge: cannot import %s — %s", _SYMBOL_MODULE_PATH, exc)
            return None
    try:
        return getattr(_SYMBOL_MODULE, name)
    except AttributeError:
        _log.warning("bridge: symbol %r not found in %s — skipping subscription", name, _SYMBOL_MODULE_PATH)
        return None


# ── Typed event routing table ────────────────────────────────────────────────
# Maps each typed event class name to the handler method that should receive it.
# The handler receives a dict payload converted from the typed event via
# event.to_dict().  Events with lambda no-op handlers in the old subscription
# block are deliberately omitted.
# Order matches the old setup_subscriptions() block for maintainability.
# ---------------------------------------------------------------------------

TYPED_EVENT_ROUTING: list[tuple[str, str]] = [
    ("SystemSettings", "_on_system_settings"),
    ("OrchestratorStartup", "_on_orchestrator_startup"),
    ("ProviderStatusChanged", "_on_provider_status"),
    ("ProviderUnavailable", "_on_provider_unavailable"),
    ("ProviderModelsList", "_on_models_list"),
    ("ProviderModelsCached", "_on_models_list"),
    ("ModelRouting", "_on_model_routing"),
    ("ModelResponse", "_on_model_response"),
    ("ModelToken", "_on_model_token"),
    ("ResponseStreamChunk", "_on_stream_chunk"),
    ("ToolExecuteStart", "_on_tool_start"),
    ("ToolExecuteFinish", "_on_tool_finish"),
    ("ToolExecuteError", "_on_tool_error"),
    ("FileDiffPreview", "_on_diff_preview"),
    ("FileModified", "_on_file_modified"),
    ("FileDeleted", "_on_file_deleted"),
    ("PlanProgress", "_on_plan_progress"),
    ("PlanRequested", "_on_plan_requested"),
    ("SessionNew", "_on_session_new"),
    ("SessionHydrated", "_on_session_hydrated"),
    ("SessionHealthAlert", "_on_session_health"),
    ("UiNotification", "_on_ui_notification"),
    ("LogEntry", "_on_log_new"),
    ("TokenBudgetUpdate", "_on_token_budget"),
    ("TokenBudgetWarning", "_on_token_budget_warning"),
    ("TokenBudget", "_on_token_budget"),
    ("ProviderContextWindow", "_on_provider_context_window"),
    ("RoleTransition", "_on_role_transition"),
    ("PreviewPending", "_on_preview_pending"),
    ("GitBranch", "_on_git_branch"),
    ("RetryAttempt", "_on_retry_attempt"),
    ("RetrySucceeded", "_on_retry_succeeded"),
    ("RetryFailed", "_on_retry_failed"),
    ("ContextDegraded", "_on_context_degraded"),
    ("ContextCompacted", "_on_context_compacted"),
    ("TaskQueueUpdated", "_on_task_queue_updated"),
    ("StepStart", "_on_step_start"),
    ("StepFinish", "_on_step_finish"),
    ("McpServerStatus", "_on_mcp_server_status"),
    ("ToolPermissionRequired", "_on_tool_permission_required"),
    ("SpawnPermissionRequired", "_on_spawn_permission_required"),
    ("UsageTurnSummary", "_on_usage_turn_summary"),
    ("UsageSubagentCost", "_on_subagent_cost"),
    ("ToolDoomLoopDetected", "_on_doom_loop_detected"),
    ("AgentMessage", "_on_agent_message"),
    ("DelegationStart", "_on_delegation_start"),
    ("DelegationFinish", "_on_delegation_finish"),
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
        for cls_name, method_name in TYPED_EVENT_ROUTING:
            event_cls = _resolve_event_class(cls_name)
            if event_cls is None:
                skipped += 1
                continue
            try:
                self._subscribe_typed(event_cls, self._make_typed_adapter(method_name))
                imported += 1
            except Exception as exc:
                logger.warning("bridge: failed to subscribe %s: %s", cls_name, exc)
                skipped += 1
        if imported:
            logger.info(
                "MessageBus: subscribed %d typed events (%d skipped)",
                imported, skipped,
            )

    def setup_subscriptions(self) -> None:
        """Subscribe to every event (via MessageBus only, Phase 5)."""
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
        # Unsubscribe typed MessageBus handlers (Phase 5: only MessageBus)
        typed_bus = getattr(self, "_typed_bus", None)
        if typed_bus is not None:
            for event_cls, handler in self._typed_subscriptions:
                try:
                    typed_bus.unsubscribe(event_cls, handler)
                except Exception as exc:
                    logger.warning("bridge: failed to unsubscribe %s: %s", event_cls.__name__, exc)
        self._typed_subscriptions.clear()
        logger.info("MessageBus: all subscriptions removed")
