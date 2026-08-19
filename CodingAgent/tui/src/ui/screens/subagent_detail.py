"""SubagentDetailScreen — view the conversation and metadata of a child session.

Opened when the user clicks a finished SubagentProgress widget or navigates to
a child session from SessionListScreen. Reads the persisted session JSON from
the sessions directory returned by ``src.core.paths.get_sessions_dir()``. For
dev-mode compatibility a legacy per-user fallback (``~/.coding_agent/sessions``)
is honoured when the core helpers are unavailable.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static
from textual.events import Key

_log = logging.getLogger(__name__)


_ROLE_COLORS: Dict[str, str] = {
    "user": "#3b82f6",
    "assistant": "#a855f7",
    "system": "#666666",
    "tool": "#22c55e",
    "tool_execution_result": "#22c55e",
}


class SubagentDetailScreen(ModalScreen[None]):
    """Read-only view of a completed subagent's session messages."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    SubagentDetailScreen {
        align: center middle;
    }
    #detail_dialog {
        width: 80;
        height: auto;
        max-height: 40;
        background: #252526;
        border: tall #a855f7;
        padding: 1 2;
    }
    #detail_title {
        text-style: bold;
        color: #a78bfa;
        margin-bottom: 1;
    }
    #detail_meta {
        color: #808080;
        margin-bottom: 1;
    }
    #detail_scroll {
        height: auto;
        max-height: 28;
        background: #252526;
    }
    .msg_row {
        margin-bottom: 1;
    }
    #detail_hint {
        color: #808080;
        height: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        child_session_id: str,
        role: str,
        task: str,
        sessions_dir: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._child_session_id = child_session_id
        self._role = role
        self._task = task
        # Cross-platform sessions directory — prefer core.paths.get_sessions_dir()
        from .._core_paths_loader import get_sessions_dir as _get_sessions_dir_helper

        default_sessions = _get_sessions_dir_helper()
        self._sessions_dir = sessions_dir or default_sessions
        self._session_data: Optional[Dict[str, Any]] = None

    def _load_session(self) -> Optional[Dict[str, Any]]:
        path = self._sessions_dir / f"session_{self._child_session_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as _err:
            _log.warning("could not load subagent session %s: %s", path.name, _err)
            return None

    def compose(self) -> ComposeResult:
        self._session_data = self._load_session()
        with Container(id="detail_dialog"):
            yield Label(f"Subagent: {self._role}", id="detail_title")
            # Meta line
            if self._session_data:
                ts = self._session_data.get("timestamp", 0)
                date_str = (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
                )
                ok = self._session_data.get("ok", True)
                status = (
                    "[bold #22c55e]✓ completed[/]"
                    if ok
                    else "[bold #ff5555]✗ failed[/]"
                )
                n_msgs = self._session_data.get("message_count", 0)
                meta = (
                    f"{date_str}  ·  {status}  ·  {n_msgs} messages\n"
                    f"[dim]Task:[/dim] {self._task[:72]}"
                )
                yield Static(meta, id="detail_meta", markup=True)
            else:
                yield Static(
                    f"[dim]Task:[/dim] {self._task[:72]}\n"
                    f"[dim]Session file not yet available (still running or not saved)[/dim]",
                    id="detail_meta",
                    markup=True,
                )
            with VerticalScroll(id="detail_scroll"):
                if self._session_data:
                    messages: List[Any] = self._session_data.get("messages", [])
                    if not messages:
                        yield Static("[dim]No messages recorded.[/dim]", markup=True)
                    for msg in messages:
                        if isinstance(msg, dict):
                            role = msg.get("role", "unknown")
                            content = msg.get("content", "")
                        elif isinstance(msg, (list, tuple)) and len(msg) == 2:
                            role, content = msg[0], msg[1]
                        else:
                            continue
                        if not isinstance(content, str):
                            content = str(content)
                        color = _ROLE_COLORS.get(role, "#cccccc")
                        preview = content[:300].replace("\n", " ")
                        if len(content) > 300:
                            preview += " …"
                        yield Static(
                            f"[bold {color}]{role.upper()}[/] {preview}",
                            classes="msg_row",
                            markup=True,
                        )
                else:
                    yield Static(
                        "[dim]No session data found.  The subagent session is saved to disk\n"
                        "only after the subagent completes.[/dim]",
                        markup=True,
                    )

            yield Static(
                f"Esc close  ·  session id: {self._child_session_id[:16]}…",
                id="detail_hint",
            )

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss()

    def action_close(self) -> None:
        self.dismiss()
