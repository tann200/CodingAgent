"""AppStatusHandlersMixin — token, context, provider, notification handlers.

Extracted from ``tui/src/ui/app.py`` (lines 1613–1822) to reduce AgentApp
to a ≤400-line core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual import on
from textual.widgets import Static

from .bus import (
    ContextCompactedEvent,
    ContextDegradedEvent,
    FileModifiedEvent,
    GitBranchEvent,
    NotificationEvent,
    ProviderStatusChangeEvent,
    RetryAttemptEvent,
    RetryFailedEvent,
    RetrySucceededEvent,
    RoleTransitionEvent,
    SessionHealthEvent,
    StatusUpdate,
    TaskEscalatedEvent,
    TaskQueueUpdatedEvent,
    TokenBudgetEvent,
    TokenUsageEvent,
)
from .components import SideBySideDiff
from .logging import get_logger

from ._app_protocol import AgentAppProtocol

if TYPE_CHECKING:
    from textual.notifications import SeverityLevel

logger = get_logger("app_status")

_COST_INPUT_PER_1K: float = 0.001
_COST_OUTPUT_PER_1K: float = 0.003

def _budget_color(percent: float) -> str:
    if percent >= 86:
        return "#ff5555"
    if percent >= 61:
        return "#facc15"
    return "#22c55e"

class AppStatusHandlersMixin:
    """Token budget, git, notification, role, provider, file, task, and retry handlers.

    Expects the host class to expose:
    - ``self.total_tokens``, ``self.context_window`` (reactive ints)
    - ``self.pending_tasks``, ``self.queue_size`` (reactive ints)
    - ``self.active_role`` (reactive str)
    - ``self._session_input_tokens``, ``self._session_output_tokens`` (int)
    - ``self._modified_files`` (list[str])
    - ``self._bridge`` (AgentBridge)
    - ``self._settings`` (SettingsStore)
    - ``self._update_status_bar``, ``self._update_role_display``
    - ``self._update_provider_status_widgets``, ``self._finalize_stream``
    - ``self._update_status_text``
    - ``self._mount_chat_widget``, ``self._chat_handle_*`` delegate methods
    - ``self.query_one``, ``self.notify``
    """

    # ── Token budget (§12.4) ──────────────────────────────────────────────

    @on(TokenBudgetEvent)
    def handle_token_budget(self: AgentAppProtocol, event) -> None:
        color = _budget_color(event.percent)
        self.total_tokens = event.used
        self.context_window = event.limit
        cost = event.used / 1000 * _COST_INPUT_PER_1K
        try:
            self.query_one("#sb_tokens", Static).update(
                f"[bold {color}]{event.used:,} / {event.limit:,}  ({event.percent:.1f}%)[/]"
            )
            self.query_one("#sb_cost", Static).update(f"${cost:.4f}")
        except Exception:
            pass
        if event.warning:
            self.notify(
                f"Token budget at {event.percent:.0f}% — consider /compact",
                severity="warning",
                timeout=6,
            )
        self._update_status_bar()

    # ── Git branch (T_SIDEBAR) ─────────────────────────────────────────────

    @on(GitBranchEvent)
    def handle_git_branch(self: AgentAppProtocol, event) -> None:
        dirty_mark = " [bold #facc15]*[/]" if event.dirty else ""
        ahead_behind = ""
        if event.ahead:
            ahead_behind += f" ↑{event.ahead}"
        if event.behind:
            ahead_behind += f" ↓{event.behind}"
        dot = "[bold #22c55e]●[/]" if event.branch else "[dim]○[/]"
        try:
            self.query_one("#sb_git", Static).update(
                f"{dot} {event.branch}{ahead_behind}{dirty_mark}",
            )
        except Exception:
            pass

    # ── Old token usage (backwards compat) ────────────────────────────────

    @on(TokenUsageEvent)
    def handle_token_usage(self: AgentAppProtocol, event) -> None:
        self.total_tokens = event.total or event.total_tokens
        self.context_window = event.model_window or 32000
        used_pct = (
            (self.total_tokens / self.context_window * 100)
            if self.context_window
            else 0
        )
        color = _budget_color(used_pct)
        self._session_input_tokens += event.system + event.task + event.tools
        cost = (
            self._session_input_tokens / 1000 * _COST_INPUT_PER_1K
            + self._session_output_tokens / 1000 * _COST_OUTPUT_PER_1K
        )
        try:
            self.query_one("#sb_tokens", Static).update(
                f"[bold {color}]{self.total_tokens:,} / {self.context_window:,}  ({used_pct:.1f}%)[/]"
            )
            self.query_one("#sb_context", Static).update(
                f"In: {event.system + event.task + event.tools:,} | Out: {self._session_output_tokens:,}"
            )
            self.query_one("#sb_cost", Static).update(f"${cost:.4f}")
            self._update_status_bar()
        except Exception as e:
            logger.error(f"Error updating token display: {e}")

    # ── Notifications (§4.5 ui.notification) ─────────────────────────────

    @on(NotificationEvent)
    def handle_notification(self: AgentAppProtocol, event) -> None:
        severity_map = {
            "success": "information",
            "error": "error",
            "warning": "warning",
            "info": "information",
        }
        sev = severity_map.get(event.level, "information")
        self.notify(event.message, severity=cast("SeverityLevel", sev), timeout=5)

    # ── Session health ────────────────────────────────────────────────────

    @on(SessionHealthEvent)
    async def handle_session_health(self: AgentAppProtocol, event) -> None:
        await self._chat_handle_session_health(event)

    # ── Status + role ─────────────────────────────────────────────────────

    @on(StatusUpdate)
    def handle_status(self: AgentAppProtocol, event) -> None:
        logger.info(f"Status: {event.message}")
        self._update_status_text(event.message)
        self.notify(event.message, timeout=3)

    @on(RoleTransitionEvent)
    async def handle_role_transition(self: AgentAppProtocol, event) -> None:
        self._finalize_stream()
        self.active_role = event.to_role
        self._update_role_display(event.to_role)
        self._update_status_bar()
        logger.info(f"Role transition: {event.from_role} → {event.to_role}")
        from .status_bar import ROLE_COLORS, ROLE_LABELS

        color = ROLE_COLORS.get(event.to_role, "#888888")
        label = ROLE_LABELS.get(event.to_role, event.to_role.upper().replace("_", " "))
        from_label = ROLE_LABELS.get(
            event.from_role, event.from_role.upper().replace("_", " ")
        )
        widget = Static(
            f"[bold {color}]>> {from_label} → {label}[/]",
            classes="system_msg",
            markup=True,
        )
        await self._mount_chat_widget(widget)

    @on(ProviderStatusChangeEvent)
    def handle_provider_status(self: AgentAppProtocol, event) -> None:
        logger.info(
            f"Provider {event.provider}: {event.old_status} → {event.new_status}"
        )
        self._update_provider_status_widgets(event.provider, event.new_status)
        try:
            banner = self.query_one("#provider_banner", Static)
            banner.remove_class("connected", "error")
            if event.new_status == "connected":
                banner.add_class("connected")
            elif event.new_status in ("error", "failed"):
                banner.add_class("error")
        except Exception:
            pass

    @on(TaskQueueUpdatedEvent)
    def handle_task_queue(self: AgentAppProtocol, event) -> None:
        self.pending_tasks = event.pending_count
        self.queue_size = event.queue_size
        try:
            self.query_one("#sb_session", Static).update(
                f"Pending: {event.pending_count} | Queue: {event.queue_size}"
            )
        except Exception:
            pass

    # ── File modification ─────────────────────────────────────────────────

    @on(FileModifiedEvent)
    async def handle_file_modified(self: AgentAppProtocol, event) -> None:
        logger.info(f"File modified: {event.file_path}")
        if event.file_path and event.file_path not in self._modified_files:
            self._modified_files.append(event.file_path)
        try:
            lines = []
            for fp in self._modified_files[-5:]:
                if fp.startswith("[deleted]"):
                    lines.append(f"[bold #ff5555]✗[/] {fp[9:].strip()}")
                else:
                    lines.append(f"[#22c55e]✓[/] {fp}")
            self.query_one("#sb_files", Static).update(
                "\n".join(lines) if lines else "None"
            )
        except Exception:
            pass
        if event.diff:
            existing = [
                w
                for w in self.query(SideBySideDiff)
                if getattr(w, "_path", None) == event.file_path
            ]
            if not existing:
                from .components import AgentArtifact

                artifact = AgentArtifact(
                    content=event.diff, title=event.file_path, kind="diff"
                )
                await self._mount_chat_widget(artifact)

    # ── Task escalation ───────────────────────────────────────────────────

    @on(TaskEscalatedEvent)
    async def handle_task_escalated(self: AgentAppProtocol, event) -> None:
        logger.warning(f"Task escalated: {event.task_id} - {event.reason}")
        widget = Static(
            f"[bold #ff5555]Escalation:[/] Task {event.task_id} — {event.reason} (retry {event.retry_count})",
            classes="error_msg",
            markup=True,
        )
        await self._mount_chat_widget(widget)

    # ── Context compaction/degradation ────────────────────────────────────

    @on(ContextCompactedEvent)
    async def handle_context_compacted(self: AgentAppProtocol, event) -> None:
        await self._chat_handle_context_compacted(event)

    @on(ContextDegradedEvent)
    async def handle_context_degraded(self: AgentAppProtocol, event) -> None:
        await self._chat_handle_context_degraded(event)

    # ── Retry events ──────────────────────────────────────────────────────

    @on(RetryAttemptEvent)
    async def handle_retry_attempt(self: AgentAppProtocol, event) -> None:
        await self._chat_handle_retry_attempt(event)

    @on(RetrySucceededEvent)
    async def handle_retry_succeeded(self: AgentAppProtocol, event) -> None:
        await self._chat_handle_retry_succeeded(event)

    @on(RetryFailedEvent)
    async def handle_retry_failed(self: AgentAppProtocol, event) -> None:
        await self._chat_handle_retry_failed(event)
