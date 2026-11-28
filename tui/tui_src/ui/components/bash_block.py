"""bash_block.py — Native Textual 8.1.0 Collapsible bash-output block widget (TASK-38)."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Collapsible, Label, Static

from ..terminal_sanitize import strip_terminal_controls

MAX_VISIBLE_LINES = 40


class BashBlock(Vertical):
    """Theme-aware collapsible widget for a single bash tool-call result."""

    DEFAULT_CSS = """
    BashBlock {
        layout: vertical;
        height: auto;
        border: round $panel;
        background: $surface;
        padding: 0 1;
        margin-bottom: 1;
    }
    BashBlock Collapsible {
        background: transparent;
        border: none;
        padding: 0;
    }
    BashBlock .bash-cmd {
        color: $warning;
        text-style: bold;
    }
    BashBlock .bash-output {
        color: $text;
    }
    BashBlock .bash-truncated {
        color: $text-muted;
    }
    """

    collapsed: reactive[bool] = reactive(True)

    def __init__(self, description: str = "", command: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._description = strip_terminal_controls(description)
        self._command = strip_terminal_controls(command)
        self._output_lines: list[str] = []

    def _title_text(self) -> str:
        desc = self._description or "bash"
        if self._command:
            short_cmd = self._command if len(self._command) <= 64 else self._command[:61] + "..."
            return f"{desc} — $ {short_cmd}"
        return desc

    def compose(self) -> ComposeResult:
        children: list[Any] = []
        if self._command:
            children.append(Label(f"$ {self._command}", classes="bash-cmd", markup=False))
        children.append(Static("", classes="bash-output", markup=False))
        yield Collapsible(*children, title=Text(self._title_text()), collapsed=False, classes="bash-collapsible")  # type: ignore[arg-type]

    def on_mount(self) -> None:
        self._refresh_output()

    def append_output(self, line: str) -> None:
        """Append a single output line and refresh the visible area."""
        self._output_lines.append(strip_terminal_controls(line))
        if self.is_mounted:
            self._refresh_output()

    def set_output(self, lines: list[str]) -> None:
        """Replace all output lines at once."""
        self._output_lines = [strip_terminal_controls(ln) for ln in lines]
        if self.is_mounted:
            self._refresh_output()

    @on(Collapsible.Toggled)
    def _on_collapsible_toggled(self, event: Collapsible.Toggled) -> None:
        # Expanding/collapsing updates truncation state cleanly
        if not event.collapsible.collapsed and self.collapsed and len(self._output_lines) > MAX_VISIBLE_LINES:
            self.collapsed = False

    def on_click(self) -> None:
        if len(self._output_lines) > MAX_VISIBLE_LINES:
            self.collapsed = not self.collapsed

    def watch_collapsed(self, value: bool) -> None:  # noqa: FBT001
        if self.is_mounted:
            self._refresh_output()

    def _refresh_output(self) -> None:
        try:
            output_widget = self.query_one(".bash-output", Static)
        except Exception:
            return
        lines = self._output_lines
        if not lines:
            output_widget.update("")
            return

        t = Text()
        if self.collapsed and len(lines) > MAX_VISIBLE_LINES:
            visible = lines[:MAX_VISIBLE_LINES]
            hidden = len(lines) - MAX_VISIBLE_LINES
            t.append("\n".join(visible))
            t.append(f"\n... {hidden} more line(s) — click to expand", style="dim")
        else:
            t.append("\n".join(lines))
            if len(lines) > MAX_VISIBLE_LINES:
                t.append("\n(click to collapse)", style="dim")
        output_widget.update(t)

    @property
    def output_lines(self) -> list[str]:
        return list(self._output_lines)

    @property
    def is_truncated(self) -> bool:
        """True when collapsed and output exceeds MAX_VISIBLE_LINES."""
        return self.collapsed and len(self._output_lines) > MAX_VISIBLE_LINES
