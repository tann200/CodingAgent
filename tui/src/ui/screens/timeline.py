"""TimelineScreen — scrollable timeline of all turns in the current session.

Accepts `history: list[tuple[str, str]]` so it never needs src.core.
No direct src.core imports — standalone TUI architecture.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static
from textual.events import Key


class TimelineScreen(ModalScreen[None]):
    """Scrollable timeline of all (role, content) turns in the current session."""

    BINDINGS = [
        ("escape", "close_timeline", "Close"),
        ("up", "prev_turn", "Up"),
        ("down", "next_turn", "Down"),
    ]

    DEFAULT_CSS = """
    TimelineScreen {
        align: center middle;
    }
    #timeline_dialog {
        width: 82;
        height: 42;
        background: #1e1e1e;
        border: tall #555555;
        padding: 1 2;
        layout: vertical;
    }
    #timeline_title {
        text-style: bold;
        color: #d4d4d4;
        margin-bottom: 1;
    }
    #timeline_search {
        margin-bottom: 1;
    }
    #timeline_list {
        height: 1fr;
        overflow-y: scroll;
    }
    #timeline_hint {
        height: 1;
        color: #808080;
    }
    """

    def __init__(
        self,
        history: List[Tuple[str, str]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Convert raw history tuples into message dicts
        self._messages: List[Dict[str, Any]] = [
            {"role": role, "content": content, "index": i}
            for i, (role, content) in enumerate(history)
            if role in ("user", "assistant") and content
        ]
        self._filtered: List[Dict[str, Any]] = list(self._messages)
        self._selected: int = max(0, len(self._filtered) - 1)

    def compose(self) -> ComposeResult:
        with Container(id="timeline_dialog"):
            yield Label("Session Timeline", id="timeline_title")
            yield Input(placeholder="Filter messages...", id="timeline_search")
            yield Static("", id="timeline_list")
            yield Static(
                "Up/Down navigate  Enter jump to  Esc close",
                id="timeline_hint",
            )

    def on_mount(self) -> None:
        self._render_list()
        try:
            self.query_one("#timeline_search", Input).focus()
        except Exception:
            pass

    def _filter(self, query: str) -> None:
        q = query.strip().lower()
        if not q:
            self._filtered = list(self._messages)
        else:
            self._filtered = [m for m in self._messages if q in m["content"].lower()]
        self._selected = 0
        self._render_list()

    def _render_list(self) -> None:
        try:
            widget = self.query_one("#timeline_list", Static)
        except Exception:
            return
        if not self._filtered:
            widget.update("[dim]No messages found.[/dim]")
            return
        lines = []
        for i, m in enumerate(self._filtered):
            role = m["role"]
            content = str(m["content"])
            preview = content[:120].replace("\n", " ")
            if len(content) > 120:
                preview += "…"
            preview = markup_escape(preview)
            role_markup = (
                "[blue]User[/blue]" if role == "user" else "[green]Asst[/green]"
            )
            if i == self._selected:
                lines.append(
                    f"[bold white on #005f87] {role_markup} {preview} [/bold white on #005f87]"
                )
            else:
                lines.append(f" {role_markup} [dim]{preview}[/dim]")
        widget.update("\n".join(lines))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "timeline_search":
            self._filter(event.value)

    def on_key(self, event: Key) -> None:
        if event.key == "up":
            self._selected = max(0, self._selected - 1)
            self._render_list()
            event.prevent_default()
        elif event.key == "down":
            self._selected = min(len(self._filtered) - 1, self._selected + 1)
            self._render_list()
            event.prevent_default()
        elif event.key == "enter":
            self._jump_to_selected()
            event.prevent_default()

    def _jump_to_selected(self) -> None:
        """Write a brief marker to the main chat output and close."""
        if not self._filtered or self._selected >= len(self._filtered):
            self.dismiss()
            return
        m = self._filtered[self._selected]
        role = m["role"]
        content = str(m["content"])
        try:
            # Best-effort: post a status message into the chat log via app
            snippet = markup_escape(content[:200].replace("\n", " "))
            prefix = "[blue]User:[/blue]" if role == "user" else "[green]Asst:[/green]"
            from textual.widgets import Static as _Static

            chat_log = self.app.query_one("#chat_log")
            self.app.call_later(
                lambda: chat_log.mount(
                    _Static(
                        f"[bold cyan]── timeline jump ──[/bold cyan]\n{prefix} {snippet}",
                        markup=True,
                    )
                )
            )
        except Exception:
            pass
        self.dismiss()

    def action_close_timeline(self) -> None:
        self.dismiss()

    def action_prev_turn(self) -> None:
        self._selected = max(0, self._selected - 1)
        self._render_list()

    def action_next_turn(self) -> None:
        self._selected = min(len(self._filtered) - 1, self._selected + 1)
        self._render_list()
