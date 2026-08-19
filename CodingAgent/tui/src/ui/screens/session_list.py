"""SessionListScreen — browse, resume, and delete saved sessions.

Sessions live in the sessions directory returned by ``src.core.paths.get_sessions_dir()``.
For historical/dev-mode compatibility a legacy per-user fallback (``~/.coding_agent/sessions``) is honoured when the core helpers are unavailable.
No src.core imports — communicates only via self.app methods.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import List, Tuple, Any, Dict

from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static
from textual.events import Key

_log = logging.getLogger(__name__)


class SessionListScreen(ModalScreen[None]):
    """Browse, resume, and delete saved sessions."""

    BINDINGS = [
        ("escape", "close_sessions", "Close"),
        ("n", "new_session", "New"),
    ]

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

    def __init__(self, filter_subagents: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # GAP-FOOTER-3: if True, only show child/subagent sessions in the list
        self._filter_subagents = filter_subagents
        # list of (path, payload) sorted newest-first
        self._all_sessions: List[Tuple[Path, Dict[str, Any]]] = []
        self._filtered: List[Tuple[Path, Dict[str, Any]]] = []
        self._selected: int = 0
        self._show_new_session: bool = True

    def compose(self) -> ComposeResult:
        with Container(id="sessions_dialog"):
            yield Label("Sessions", id="sessions_title")
            yield Input(placeholder="Search sessions...", id="sessions_search")
            yield Static("", id="sessions_list")
            yield Static(
                "↑/↓ navigate  Enter resume  n new session  d delete  Esc close",
                id="sessions_hint",
            )

    def on_mount(self) -> None:
        self._load_sessions()
        self._render_list()
        try:
            title = self.query_one("#sessions_title", Label)
            title.update("Subagent Sessions" if self._filter_subagents else "Sessions")
        except Exception:
            pass
        try:
            self.query_one("#sessions_search", Input).focus()
        except Exception:
            pass

    def _load_sessions(self) -> None:
        try:
            _get_sessions_dir = getattr(self.app, "_get_sessions_dir", None)
            if _get_sessions_dir is None:
                _log.warning("app has no _get_sessions_dir — session list will be empty")
                return
            sessions_dir: Any = _get_sessions_dir()
            if not sessions_dir:
                return
            files = sorted(Path(sessions_dir).glob("session_*.json"), reverse=True)
            self._all_sessions = []
            for f in files[:100]:
                try:
                    payload: Dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))
                    self._all_sessions.append((f, payload))
                except Exception as _parse_err:
                    _log.warning("skipping corrupt session file %s: %s", f.name, _parse_err)
            # GAP-FOOTER-3: pre-filter to subagent sessions only when requested
            if self._filter_subagents:
                self._all_sessions = [
                    (p, d) for p, d in self._all_sessions if d.get("parent_session_id")
                ]
            self._filtered = list(self._all_sessions)
        except Exception:
            pass

    def _filter(self, query: str) -> None:
        q = query.lower()
        self._filtered = [
            (p, data)
            for p, data in self._all_sessions
            if not q
            or q in data.get("task_name", "").lower()
            or q in str(p).lower()
            or q in data.get("role", "").lower()
        ]
        # Hide "new session" entry when actively searching
        self._show_new_session = not q
        self._selected = 0
        self._render_list()

    def _render_list(self) -> None:
        lines = []
        # "Start new session" entry at the top (hidden when searching)
        if self._show_new_session:
            if self._selected == 0:
                lines.append("[bold white on #005f87]  ➕ Start new session [/bold white on #005f87]")
            else:
                lines.append("[bold #4fc1ff]  ➕ Start new session [/bold #4fc1ff]")
        if not self._filtered:
            lines.append("[dim]No sessions found.[/dim]")
        # offset: if "new session" entry is shown, session items start at index 1
        _offset = 1 if self._show_new_session else 0
        for i, (_, data) in enumerate(self._filtered[:20]):
            ts = data.get("timestamp", 0)
            date_str = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
            )
            task = str(data.get("task_name", "?"))[:36]
            msgs = data.get("message_count", 0)
            # GAP-FOOTER-3: annotate child (subagent) sessions with role + status
            is_child = bool(data.get("parent_session_id"))
            role_tag = ""
            ok_tag = ""
            if is_child:
                role = data.get("role", "subagent")
                ok = data.get("ok", True)
                ok_tag = " [bold #22c55e]✓[/]" if ok else " [bold #ff5555]✗[/]"
                role_tag = f"[dim #a78bfa] ↳ {role}[/] "
            indent = "  " if is_child else ""
            if i + _offset == self._selected:
                lines.append(
                    f"[bold white on #005f87]{indent}{role_tag}{date_str}  {task:<36} {msgs}m{ok_tag}[/bold white on #005f87]"
                )
            else:
                lines.append(
                    f"{indent}{role_tag}[dim]{date_str}[/dim]  [cyan]{task:<36}[/cyan] [dim]{msgs}m[/dim]{ok_tag}"
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
            _max = len(self._filtered) + (1 if self._show_new_session else 0) - 1
            self._selected = min(max(0, _max), self._selected + 1)
            self._render_list()
            event.prevent_default()
        elif event.key == "enter":
            self._open_selected()
            event.prevent_default()
        elif event.key == "d":
            self._delete_selected()
        elif event.key == "n":
            self._start_new_session()

    def _start_new_session(self) -> None:
        """Signal the app to start a new session and close this modal."""
        try:
            from tui.tui_src.ui.events import SlashCommand
            self.app.post_message(SlashCommand("new"))
        except Exception:
            pass
        self.dismiss()

    def _open_selected(self) -> None:
        """Route to detail screen for subagent sessions; resume for parent sessions."""
        if not self._filtered:
            return
        # "Start new session" entry selected
        if self._show_new_session and self._selected == 0:
            self._start_new_session()
            return
        _offset = 1 if self._show_new_session else 0
        idx = self._selected - _offset
        if idx < 0 or idx >= len(self._filtered):
            return
        _, data = self._filtered[idx]
        if data.get("parent_session_id"):
            # Child session — open detail view
            from .subagent_detail import SubagentDetailScreen

            sessions_dir = getattr(self.app, "_get_sessions_dir", lambda: None)()
            self.app.push_screen(
                SubagentDetailScreen(
                    child_session_id=data.get("session_id", ""),
                    role=data.get("role", "subagent"),
                    task=data.get("task_name", ""),
                    sessions_dir=sessions_dir,
                )
            )
            return
        self._resume_selected()

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
        self.dismiss()

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
        self.dismiss()

    def action_new_session(self) -> None:
        self._start_new_session()
