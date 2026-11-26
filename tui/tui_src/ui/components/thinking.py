"""ThinkingProcess — native Textual 8.1.0 Collapsible reasoning widget (TASK-38)."""

from __future__ import annotations

import logging
import time
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Collapsible, Markdown

from ..terminal_sanitize import strip_terminal_controls

_log = logging.getLogger(__name__)


class ThinkingProcess(Vertical):
    """Collapsible reasoning-chain widget backed by native Textual ``Collapsible``."""

    DEFAULT_CSS = """
    ThinkingProcess {
        background: $surface;
        border-left: wide $accent;
        margin: 1 0;
        height: auto;
        padding: 0 1;
    }
    ThinkingProcess.pulse {
        border-left: wide $primary;
    }
    ThinkingProcess.expanded {
        border-left: wide $primary;
        background: $panel;
    }
    ThinkingProcess Collapsible {
        background: transparent;
        border: none;
        padding: 0;
    }
    ThinkingProcess Markdown {
        color: $text-muted;
        background: transparent;
        padding: 0 1;
    }
    """

    def __init__(self, content: str, start_time: Optional[float] = None) -> None:
        super().__init__()
        self.end_time = time.monotonic()
        self.elapsed = self.end_time - (start_time or self.end_time)
        clean_text = str(content) if not isinstance(content, str) else content.replace("Thinking:", "").strip()
        self.raw_content = strip_terminal_controls(clean_text)
        self.is_expanded = False

    def _collapsed_title(self) -> str:
        from rich.markup import escape as _escape

        summary = _escape(self.raw_content[:80].replace("\n", " ")) + "..."
        return f"Thinking: {summary} [{self.elapsed:.2f}s]"

    def _expanded_title(self) -> str:
        return f"Reasoning Chain [{self.elapsed:.2f}s]"

    def compose(self) -> ComposeResult:
        yield Collapsible(
            Markdown(self.raw_content, classes="content"),
            title=self._collapsed_title(),
            collapsed=True,
            classes="thinking_collapsible",
        )

    def on_mount(self) -> None:
        self.add_class("pulse")
        self.set_timer(0.5, lambda: self.remove_class("pulse"))

    @on(Collapsible.Toggled)
    def _on_collapsible_toggled(self, event: Collapsible.Toggled) -> None:
        self.is_expanded = not event.collapsible.collapsed
        self.set_class(self.is_expanded, "expanded")
        event.collapsible.title = self._expanded_title() if self.is_expanded else self._collapsed_title()

    def _toggle_expansion(self) -> None:
        """Programmatically toggle expansion (backward-compatible helper for tests)."""
        try:
            coll = self.query_one(".thinking_collapsible", Collapsible)
            coll.collapsed = not coll.collapsed
            self.is_expanded = not coll.collapsed
            self.set_class(self.is_expanded, "expanded")
            coll.title = self._expanded_title() if self.is_expanded else self._collapsed_title()
        except Exception:
            self.is_expanded = not self.is_expanded
            self.set_class(self.is_expanded, "expanded")
