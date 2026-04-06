"""SessionListScreen — browse, resume, and delete saved sessions.

Sessions live in ~/.coding_agent/sessions/session_*.json.
No src.core imports — communicates only via self.app methods.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import List, Tuple, Any, Dict

from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static
from textual.events import Key


class SessionListScreen(ModalScreen[None]):
    """Browse, resume, and delete saved sessions."""

    BINDINGS = [("escape", "close_sessions", "Close")]

    DEFAULT_CSS = """
    SessionListScreen {
        align: center middle;
    }
    #sessions_dialog {
        width: 72;
        height: auto;
        max-height: 32;
        background: #252526;
        border: tall #007acc;
        padding: 1 2;
    }
    #sessions_title {
        text-style: bold;
        color: #4fc1ff;
        margin-bottom: 1;
    }
    #sessions_search {
        border: tall #555555;
        background: #1e1e1e;
        margin-bottom: 1;
    }
    #sessions_list {
        height: auto;
        max-height: 20;
        background: #252526;
    }
    #sessions_hint {
        color: #808080;
        height: 1;
        margin-top: 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # list of (path, payload) sorted newest-first
        self._all_sessions: List[Tuple[Path, Dict[str, Any]]] = []
        self._filtered: List[Tuple[Path, Dict[str, Any]]] = []
        self._selected: int = 0

    def compose(self) -> ComposeResult:
        with Container(id="sessions_dialog"):
            yield Label("Sessions", id="sessions_title")
            yield Input(placeholder="Search sessions...", id="sessions_search")
            yield Static("", id="sessions_list")
            yield Static(
                "Up/Down navigate  Enter resume  d delete  Esc close",
                id="sessions_hint",
            )

    def on_mount(self) -> None:
        self._load_sessions()
        self._render_list()
        try:
            self.query_one("#sessions_search", Input).focus()
        except Exception:
            pass

    def _load_sessions(self) -> None:
        try:
            sessions_dir: Any = getattr(self.app, "_get_sessions_dir", lambda: None)()
            if not sessions_dir:
                return
            files = sorted(Path(sessions_dir).glob("session_*.json"), reverse=True)
            self._all_sessions = []
            for f in files[:100]:
                try:
                    payload: Dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))
                    self._all_sessions.append((f, payload))
                except Exception:
                    pass
            self._filtered = list(self._all_sessions)
        except Exception:
            pass

    def _filter(self, query: str) -> None:
        q = query.lower()
        self._filtered = [
            (p, data)
            for p, data in self._all_sessions
            if not q or q in data.get("task_name", "").lower() or q in str(p).lower()
        ]
        self._selected = 0
        self._render_list()

    def _render_list(self) -> None:
        lines = []
        if not self._filtered:
            lines = ["[dim]No sessions found.[/dim]"]
        for i, (_, data) in enumerate(self._filtered[:20]):
            ts = data.get("timestamp", 0)
            date_str = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
            )
            task = str(data.get("task_name", "?"))[:40]
            msgs = data.get("message_count", 0)
            if i == self._selected:
                lines.append(
                    f"[bold white on #005f87] {date_str}  {task:<40} {msgs} msgs [/bold white on #005f87]"
                )
            else:
                lines.append(
                    f"  [dim]{date_str}[/dim]  [cyan]{task:<40}[/cyan] [dim]{msgs} msgs[/dim]"
                )
        try:
            self.query_one("#sessions_list", Static).update("\n".join(lines))
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        self._filter(event.value)

    def on_key(self, event: Key) -> None:
        if not self._filtered:
            return
        if event.key == "up":
            self._selected = max(0, self._selected - 1)
            self._render_list()
            event.prevent_default()
        elif event.key == "down":
            self._selected = min(len(self._filtered) - 1, self._selected + 1)
            self._render_list()
            event.prevent_default()
        elif event.key == "enter":
            self._resume_selected()
            event.prevent_default()
        elif event.key == "d":
            self._delete_selected()

    def _resume_selected(self) -> None:
        if not self._filtered or self._selected >= len(self._filtered):
            return
        _, data = self._filtered[self._selected]
        messages = data.get("messages", [])
        working_dir = data.get("working_dir", "")
        try:
            bridge = getattr(self.app, "_bridge", None)
            if bridge is not None and messages:
                # TUI-06: load history into bridge before starting new session
                with bridge._history_lock:
                    bridge.history = [
                        (m.get("role", ""), m.get("content", ""))
                        for m in messages
                        if isinstance(m, dict)
                    ]
                if working_dir:
                    bridge.working_dir = working_dir
                # Reset orchestrator state so it's fresh for the resumed session
                bridge.start_new_session()
            task = str(data.get("task_name", "session"))[:40]
            self.app.notify(f"Session resumed: {task}")
        except Exception as exc:
            self.app.notify(f"Failed to resume: {exc}", severity="error")
        self.app.pop_screen()

    def _delete_selected(self) -> None:
        if not self._filtered or self._selected >= len(self._filtered):
            return
        path, _ = self._filtered[self._selected]
        try:
            trash = path.parent / "trash"
            trash.mkdir(exist_ok=True)
            shutil.move(str(path), str(trash / path.name))
            self._filtered.pop(self._selected)
            self._all_sessions = [(p, d) for p, d in self._all_sessions if p != path]
            self._selected = min(self._selected, max(0, len(self._filtered) - 1))
            self._render_list()
            self.app.notify("Session moved to trash.")
        except Exception as exc:
            self.app.notify(f"Delete failed: {exc}", severity="error")

    def action_close_sessions(self) -> None:
        self.app.pop_screen()
