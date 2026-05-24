"""BridgeContextMixin — context, token budget, session health, and step event handlers.

Contains: _on_token_budget, _on_token_budget_warning, _on_context_degraded,
_on_context_compacted, _on_task_queue_updated, _on_step_start, _on_step_finish,
_on_role_transition, _on_preview_pending, _on_git_branch, _on_retry_attempt,
_on_retry_succeeded, _on_retry_failed, _on_session_new, _on_session_hydrated,
_on_session_health, _on_ui_notification, _on_log_new, _on_usage_turn_summary,
_on_subagent_cost, _get_active_context_length, compact_context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class BridgeContextMixin:
    """Mixin providing context window, token budget, and session event handlers."""

    def _get_active_context_length(self) -> int:
        """Return the active provider's context length for use as a token-budget
        fallback when the event payload does not include a limit field.

        Reads provider_context._load_active_context_length() which prefers the
        live value set by set_active_context_length() over the static providers.json
        entry.  Falls back to 32_768 if the import fails.
        """
        try:
            from src.core.inference.provider_context import (  # type: ignore[import]
                _load_active_context_length,
            )

            return _load_active_context_length()
        except Exception:
            return 32_768

    def _on_token_budget(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import TokenBudgetEvent

        # Orchestrator publishes used_tokens/max_tokens; mock uses used/limit.
        used = payload.get("used") or payload.get("used_tokens", 0)
        _default_limit = self._get_active_context_length()
        limit = (
            payload.get("limit")
            or payload.get("max_tokens", _default_limit)
            or _default_limit
        )
        pct = payload.get("percent") or payload.get("usage_ratio", 0.0)
        # usage_ratio is 0..1, percent is 0..100 — normalise to 0..100
        if pct and pct <= 1.0:
            pct = pct * 100
        self._post(
            TokenBudgetEvent(
                used=int(used),
                limit=int(limit),
                percent=float(pct),
                warning=False,
            )
        )

    def _on_token_budget_warning(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import TokenBudgetEvent

        used = payload.get("used") or payload.get("used_tokens", 0)
        _default_limit = self._get_active_context_length()
        limit = (
            payload.get("limit")
            or payload.get("max_tokens", _default_limit)
            or _default_limit
        )
        pct = payload.get("percent") or payload.get("usage_ratio", 0.0)
        if pct and pct <= 1.0:
            pct = pct * 100
        self._post(
            TokenBudgetEvent(
                used=int(used),
                limit=int(limit),
                percent=float(pct),
                warning=True,
            )
        )

    def _on_context_degraded(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import ContextDegradedEvent

        self._post(
            ContextDegradedEvent(
                target_window=payload.get("target_window", 0),
                reason=payload.get("reason", ""),
            )
        )

    def _on_context_compacted(self, payload: dict) -> None:
        """S9-B / TASK-TUI-9: Notify UI and insert compaction divider in chat log."""
        from tui.tui_src.ui.bus import NotificationEvent, ContextCompactedEvent

        msg = payload.get("message", "Context compacted")
        self._post(
            NotificationEvent(
                level="information",
                message=msg,
                source="compact",
            )
        )
        # Also fire the dedicated event so app.py can insert a visual divider
        # into the chat log at the correct position (not just a toast notification).
        self._post(ContextCompactedEvent(message=msg))

    def _on_task_queue_updated(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import TaskQueueUpdatedEvent

        self._post(
            TaskQueueUpdatedEvent(
                queue_size=payload.get("queue_size", 0),
                pending_count=payload.get("pending_count", 0),
                task_id=payload.get("task_id"),
                old_status=payload.get("old_status"),
                new_status=payload.get("new_status"),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_step_start(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import StepStartEvent

        self._post(
            StepStartEvent(
                tool=payload.get("tool", ""),
                step=int(payload.get("step", 0) or 0),
                total=int(payload.get("total", 0) or 0),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_step_finish(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import StepFinishEvent

        elapsed = payload.get("elapsed_ms")
        self._post(
            StepFinishEvent(
                tool=payload.get("tool", "?"),
                ok=payload.get("ok", True),
                elapsed_ms=int(elapsed) if elapsed is not None else None,
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_role_transition(self, payload: dict) -> None:
        """Real backend fires role.transition via EventBus (mock uses direct post_message)."""
        from tui.tui_src.ui.bus import RoleTransitionEvent

        self._post(
            RoleTransitionEvent(
                from_role=payload.get("from_role", "system"),
                to_role=payload.get("to_role", "lead_architect"),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_preview_pending(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import DiffPreviewEvent

        self._post(
            DiffPreviewEvent(
                path=payload.get("path") or payload.get("tool", ""),
                diff=payload.get("diff", ""),
                is_new_file=payload.get("is_new_file", False),
            )
        )

    def _on_git_branch(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import GitBranchEvent

        self._post(
            GitBranchEvent(
                branch=payload.get("branch", "main"),
                dirty=payload.get("dirty", False),
                ahead=payload.get("ahead", 0),
                behind=payload.get("behind", 0),
            )
        )

    def _on_retry_attempt(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import RetryAttemptEvent

        self._post(
            RetryAttemptEvent(
                attempt_number=payload.get("attempt_number", 0),
                max_attempts=payload.get("max_attempts", 0),
                error_type=payload.get("error_type", ""),
                provider=payload.get("provider", ""),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_retry_succeeded(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import RetrySucceededEvent

        self._post(
            RetrySucceededEvent(
                attempt_number=payload.get("attempt_number", 0),
                provider=payload.get("provider", ""),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_retry_failed(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import RetryFailedEvent

        self._post(
            RetryFailedEvent(
                total_attempts=payload.get("total_attempts", 0),
                error_type=payload.get("error_type", ""),
                provider=payload.get("provider", ""),
                run_id=payload.get("run_id", ""),
            )
        )

    def _on_session_new(self, payload: dict) -> None:
        self._schedule_callback(self.app._handle_session_new)

    def _on_session_hydrated(self, payload: dict) -> None:
        import logging as _logging
        _logging.getLogger("bridge").info("Session hydrated from backend")

    def _on_session_health(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import SessionHealthEvent

        self._post(
            SessionHealthEvent(
                level=payload.get("level", "info"),
                title=payload.get("title", ""),
                message=payload.get("message", ""),
            )
        )

    def _on_ui_notification(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import NotificationEvent

        self._post(
            NotificationEvent(
                level=payload.get("level", "info"),
                message=payload.get("message", ""),
                source=payload.get("source", ""),
            )
        )

    def _on_log_new(self, payload: dict) -> None:
        """§16.4 — write DIRECTLY to console panel; never through Python logging."""
        level = payload.get("level", "INFO").upper()
        logger_name = payload.get("logger", "")
        msg = payload.get("message", "")
        line = f"[{level}] {logger_name}: {msg}" if logger_name else f"[{level}] {msg}"
        self._schedule_callback(self.app._append_log_line, line, level)

    def _on_usage_turn_summary(self, payload: dict) -> None:
        """TUI-T6: forward per-turn token/cost summary to TUI."""
        from tui.tui_src.ui.bus import UsageTurnSummaryEvent

        self._post(
            UsageTurnSummaryEvent(
                input_tokens=int(payload.get("input_tokens", 0)),
                output_tokens=int(payload.get("output_tokens", 0)),
                model=str(payload.get("model", "")),
                cost_usd=float(payload.get("cost_usd", 0.0)),
            )
        )

    def _on_subagent_cost(self, payload: dict) -> None:
        """GAP-NEW-7: accumulate child session cost into the parent session total.

        Publishes a UsageTurnSummaryEvent with 0 tokens so the cost panel
        reflects subagent spend without creating a phantom model turn.
        """
        from tui.tui_src.ui.bus import UsageTurnSummaryEvent

        child_cost = float(payload.get("cost_usd", 0.0))
        if child_cost <= 0:
            return
        role = str(payload.get("role", "subagent"))
        self._post(
            UsageTurnSummaryEvent(
                input_tokens=0,
                output_tokens=0,
                model=f"[{role}]",
                cost_usd=child_cost,
            )
        )

    def compact_context(self) -> bool:
        """Attempt to compact context on the orchestrator. Returns True if successful."""
        orch = self._orchestrator
        if not orch:
            return False
        for method in ("compact_context", "compact", "flush_execution_trace"):
            fn = getattr(orch, method, None)
            if callable(fn):
                try:
                    fn()
                    import logging as _logging
                    _logging.getLogger("bridge").info(f"compact_context: called orchestrator.{method}()")
                    return True
                except Exception as exc:
                    import logging as _logging
                    _logging.getLogger("bridge").warning(f"compact_context: {method}() failed: {exc}")
        return False
