"""StatusBarMixin — extracted status-bar update helpers (P1-6 Phase A).

Extracted from ``tui/src/ui/app.py`` (lines 665–701, 1179–1200, 2023–2072)
so that ``AgentApp`` can inherit these methods from a dedicated module rather
than keeping them inline.

Usage::

    class AgentApp(App, StatusBarMixin):
        ...

All methods use only ``self.query_one()``, ``self.active_role``,
``self.is_streaming``, ``self.agent_running``, ``self.total_tokens``,
``self.context_window``, and ``self._pending_perm_count`` — attributes that
exist on ``AgentApp``.  No circular imports are introduced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Static

from ..logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger("status_bar")

# Imported by the mixin so callers don't have to duplicate these dicts.
ROLE_LABELS: dict[str, str] = {
    "lead_architect": "LEAD ARCHITECT",
    "full_stack_engineer": "FULL STACK ENGINEER",
    "qa_lead": "QA LEAD",
    "system": "SYSTEM",
}

ROLE_COLORS: dict[str, str] = {
    "lead_architect": "#a855f7",
    "full_stack_engineer": "#3b82f6",
    "qa_lead": "#22c55e",
    "system": "#666666",
}


class StatusBarMixin:
    """Mixin providing status-bar update methods for ``AgentApp``.

    Expects the host class to expose:
    - ``self.query_one(selector, widget_type)``  — Textual standard
    - ``self.active_role: str``
    - ``self.is_streaming: bool``
    - ``self.agent_running: bool``
    - ``self.total_tokens: int``
    - ``self.context_window: int``
    - ``self._pending_perm_count: int``
    - ``self.sub_title: str``  (Textual App.sub_title)
    """

    # ------------------------------------------------------------------
    # Permission badge (footer chip)
    # ------------------------------------------------------------------

    def _update_perm_badge(self, delta: int = 0) -> None:
        """GAP-FOOTER-1: update the pending-permission count chip in the footer."""
        self._pending_perm_count = max(0, self._pending_perm_count + delta)
        try:
            chip = self.query_one("#perm_count_chip", Static)
            if self._pending_perm_count > 0:
                chip.update(
                    f"[bold #facc15]△ {self._pending_perm_count} Permission(s)[/]"
                )
            else:
                chip.update("")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Role display (sidebar + sub_title)
    # ------------------------------------------------------------------

    def _update_role_display(self, role: str) -> None:
        """Update ``#sb_role`` sidebar widget and ``App.sub_title``."""
        label = ROLE_LABELS.get(role, role.upper().replace("_", " "))
        color = ROLE_COLORS.get(role, "#888888")
        try:
            self.query_one("#sb_role", Static).update(f"[bold {color}]{label}[/]")
            self.sub_title = f"AGENT: {label}"
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Status bar (footer left chip)
    # ------------------------------------------------------------------

    def _update_status_bar(self) -> None:
        """Refresh the ``#status_left`` footer chip with role, tokens, and state flags."""
        role = self.active_role
        label = ROLE_LABELS.get(role, role.upper().replace("_", " "))
        color = ROLE_COLORS.get(role, "#888888")
        streaming = " [bold #facc15][streaming][/]" if self.is_streaming else ""
        running = " [bold #ff5555][running][/]" if self.agent_running else ""
        try:
            self.query_one("#status_left", Static).update(
                f"[bold {color}]{label}[/] | "
                f"Tokens: {self.total_tokens:,}/{self.context_window:,}"
                f"{streaming}{running}"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # MCP status chip (footer)
    # ------------------------------------------------------------------

    def _update_mcp_status_chip(self, running: bool, count: int, has_error: bool) -> None:
        """GAP-FOOTER-2: update the MCP server status chip.

        Extracted from ``handle_mcp_status()`` so the rendering logic can be
        tested independently of the Textual event.
        """
        if running:
            if has_error:
                label = (
                    f"[bold #ff5555]⊙ MCP {count}[/]"
                    if count
                    else "[bold #ff5555]⊙ MCP[/]"
                )
            else:
                label = (
                    f"[green]⊙ MCP {count}[/green]"
                    if count
                    else "[green]⊙ MCP[/green]"
                )
        else:
            label = "[dim]⊙ MCP[/dim]"
        try:
            self.query_one("#mcp_status_chip", Static).update(label)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Provider status (sidebar)
    # ------------------------------------------------------------------

    def _update_provider_status_widgets(
        self,
        provider: str,
        new_status: str,
        model: str = "",
    ) -> None:
        """Update ``#sb_provider``, ``#sb_model_info``, and ``#provider_banner``.

        Extracted from ``handle_provider_status()`` so the rendering logic is
        testable and reusable from other event handlers.
        """
        _status_labels = {
            "connected": "connected",
            "disconnected": "not connected",
            "initializing": "initializing…",
            "unknown": "unknown",
            "error": "error",
            "failed": "error",
        }
        status_label = _status_labels.get(new_status, new_status)
        try:
            self.query_one("#sb_provider", Static).update(
                f"{provider}: {status_label}"
            )
        except Exception:
            pass
        if model:
            try:
                self.query_one("#sb_model_info", Static).update(model)
            except Exception:
                pass
            try:
                self.query_one("#provider_banner", Static).update(
                    f"  {provider}  ·  {model}"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Status text (sidebar)
    # ------------------------------------------------------------------

    def _update_status_text(self, message: str) -> None:
        """Update the ``#sb_status`` sidebar widget."""
        try:
            self.query_one("#sb_status", Static).update(f"Status: {message}")
        except Exception:
            pass
