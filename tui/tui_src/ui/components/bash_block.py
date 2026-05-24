"""bash_block.py — TASK-TUI-3

Collapsible bash-output block widget.

Each bash tool call gets its own ``BashBlock``. The header shows the description
and command. Output beyond ``MAX_VISIBLE_LINES`` is hidden with an expand hint;
clicking the header toggles the collapsed state.

Usage::

    block = BashBlock(description="Run tests", command="pytest -q")
    block.append_output("test_foo.py ... PASSED")
    block.append_output("1 passed in 0.04s")
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Static

MAX_VISIBLE_LINES = 40
_EXPAND_HINT = "[dim](... click to expand)[/]"
_COLLAPSE_HINT = "[dim](click to collapse)[/]"


class BashBlock(Widget):
    """Collapsible widget for a single bash tool-call result.

    Attributes
    ----------
    collapsed:
        When True, output beyond ``MAX_VISIBLE_LINES`` is hidden.
    """

    DEFAULT_CSS = """
    BashBlock {
        border: round #3f3f46;
        padding: 0 1;
        margin-bottom: 1;
    }
    BashBlock > .bash-header {
        color: #a1a1aa;
        text-style: bold;
    }
    BashBlock > .bash-header:hover {
        color: #e4e4e7;
        text-style: bold underline;
        cursor: pointer;
    }
    BashBlock > .bash-cmd {
        color: #fbbf24;
    }
    BashBlock > .bash-output {
        color: #d4d4d8;
    }
    BashBlock > .bash-truncated {
        color: #71717a;
    }
    """

    collapsed: reactive[bool] = reactive(True)

    def __init__(
        self,
        description: str = "",
        command: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._description = description
        self._command = command
        self._output_lines: list[str] = []

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        desc = self._description or "bash"
        yield Label(f"# {desc}", classes="bash-header")
        if self._command:
            yield Label(f"$ {self._command}", classes="bash-cmd")
        yield Static("", classes="bash-output")

    def on_mount(self) -> None:
        self._refresh_output()

    # ------------------------------------------------------------------
    # Output streaming
    # ------------------------------------------------------------------

    def append_output(self, line: str) -> None:
        """Append a single output line and refresh the visible area."""
        self._output_lines.append(line)
        if self.is_mounted:
            self._refresh_output()

    def set_output(self, lines: list[str]) -> None:
        """Replace all output lines at once."""
        self._output_lines = list(lines)
        if self.is_mounted:
            self._refresh_output()

    # ------------------------------------------------------------------
    # Reactive / click
    # ------------------------------------------------------------------

    def on_click(self) -> None:
        self.collapsed = not self.collapsed

    def watch_collapsed(self, value: bool) -> None:  # noqa: FBT001
        if self.is_mounted:
            self._refresh_output()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_output(self) -> None:
        """Update the single output Static with rendered text."""
        output_widget = self.query_one(".bash-output", Static)
        lines = self._output_lines
        if not lines:
            output_widget.update("")
            return

        if self.collapsed and len(lines) > MAX_VISIBLE_LINES:
            visible = lines[:MAX_VISIBLE_LINES]
            hidden = len(lines) - MAX_VISIBLE_LINES
            text = "\n".join(visible)
            text += f"\n[dim]... {hidden} more line(s) — click header to expand[/]"
        else:
            text = "\n".join(lines)
            if len(lines) > MAX_VISIBLE_LINES:
                text += f"\n{_COLLAPSE_HINT}"
        output_widget.update(text)

    # ------------------------------------------------------------------
    # Read-only accessors (used in tests)
    # ------------------------------------------------------------------

    @property
    def output_lines(self) -> list[str]:
        return list(self._output_lines)

    @property
    def is_truncated(self) -> bool:
        """True when collapsed and output exceeds MAX_VISIBLE_LINES."""
        return self.collapsed and len(self._output_lines) > MAX_VISIBLE_LINES
