"""SessionScreen — session browser with fork, revert, and resume actions (P2-2 / P3-2).

Extends ``SessionListScreen`` with additional actions surfaced in the UI:
- **Fork** (`f`): create an independent copy of the selected session
- **Revert** (`r`): restore the working directory to the selected session's git snapshot
- **Resume** (Enter): load the selected session's history and restart (existing behaviour
  from ``SessionListScreen._resume_selected()``)

This screen is used by:
- P3-2: ``Ctrl+R`` binding in ``AgentApp`` → ``push_screen(SessionScreen())``
- P2-2: ``/fork`` slash command can now link to a SessionScreen that shows forkable sessions

Architecture
------------
``SessionScreen`` is a drop-in replacement for ``SessionListScreen`` in all
``push_screen()`` call sites — it is backward-compatible (same constructor
signature and ``dismiss()`` contract).  The added keybindings (``f``, ``r``)
are additive and do not interfere with the parent's bindings.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from textual.app import ComposeResult
from textual.containers import Container
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from .session_list import SessionListScreen


class SessionScreen(SessionListScreen):
    """Session browser with fork, revert, and resume (P2-2 / P3-2).

    Keybindings (additive to parent):
    - ``f``: fork selected session → new independent session snapshot
    - ``r``: revert working directory to selected session's git snapshot
    - ``Enter``: resume selected session (inherited)
    - ``d``: delete/trash selected session (inherited)
    - ``Esc``: close (inherited)
    """

    BINDINGS = [
        ("escape", "close_sessions", "Close"),
        ("f", "fork_session", "Fork"),
        ("r", "revert_session", "Revert"),
    ]

    # Override hint text to show new keybindings
    _HINT_TEXT = "↑/↓ navigate  Enter resume  f fork  r revert  d delete  Esc close"

    def compose(self) -> ComposeResult:
        with Container(id="sessions_dialog"):
            yield Label("Sessions", id="sessions_title")
            yield Input(placeholder="Search sessions...", id="sessions_search")
            yield Static("", id="sessions_list")
            yield Static(self._HINT_TEXT, id="sessions_hint")

    # ------------------------------------------------------------------
    # Fork action
    # ------------------------------------------------------------------

    def action_fork_session(self) -> None:
        """Fork the selected session into a new independent session snapshot."""
        if not self._filtered or self._selected >= len(self._filtered):
            self.app.notify("No session selected.", severity="warning")
            return
        path, data = self._filtered[self._selected]
        try:
            sessions_dir = getattr(self.app, "_get_sessions_dir", lambda: None)()
            if not sessions_dir:
                raise RuntimeError("sessions directory unavailable")
            ts = int(time.time())
            fork_name = f"session_fork_{ts}.json"
            fork_path = Path(sessions_dir) / fork_name
            fork_data = dict(data)
            fork_data["forked_from"] = str(path.name)
            fork_data["timestamp"] = ts
            fork_data["task_name"] = f"[fork] {data.get('task_name', 'session')}"
            fork_path.write_text(
                json.dumps(fork_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.app.notify(f"Session forked → {fork_name}")
            self._load_sessions()
            self._render_list()
        except Exception as exc:
            self.app.notify(f"Fork failed: {exc}", severity="error")

    # ------------------------------------------------------------------
    # Revert action
    # ------------------------------------------------------------------

    def action_revert_session(self) -> None:
        """Revert the working directory to the selected session's git snapshot.

        Uses ``git stash`` + ``git checkout`` to restore the snapshot recorded
        in ``data["git_sha"]`` (written by ``_save_session_snapshot``).
        Falls back to a notification if no git SHA is stored or git is absent.
        """
        if not self._filtered or self._selected >= len(self._filtered):
            self.app.notify("No session selected.", severity="warning")
            return
        _, data = self._filtered[self._selected]
        git_sha: Optional[str] = data.get("git_sha") or data.get("snapshot_sha")
        working_dir: str = data.get("working_dir", "")
        if not git_sha:
            self.app.notify(
                "No git snapshot stored for this session.", severity="warning"
            )
            return
        if not working_dir:
            self.app.notify("No working directory recorded.", severity="warning")
            return
        try:
            cwd = Path(working_dir)
            if not cwd.is_dir():
                raise RuntimeError(f"Working directory not found: {working_dir}")
            # Stash any local changes first so the checkout doesn't fail
            subprocess.run(
                ["git", "stash", "--include-untracked"],
                cwd=str(cwd),
                capture_output=True,
                timeout=15,
            )
            result = subprocess.run(
                ["git", "checkout", git_sha],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                self.app.notify(f"Reverted to snapshot {git_sha[:8]}")
            else:
                err = (result.stderr or "").strip()[:120]
                self.app.notify(f"git checkout failed: {err}", severity="error")
        except FileNotFoundError:
            self.app.notify("git not found in PATH.", severity="error")
        except Exception as exc:
            self.app.notify(f"Revert failed: {exc}", severity="error")
        self.dismiss()

    # ------------------------------------------------------------------
    # Key handler — extends parent
    # ------------------------------------------------------------------

    def on_key(self, event: Key) -> None:
        """Route fork/revert keys; delegate everything else to parent."""
        if event.key == "f":
            self.action_fork_session()
            event.prevent_default()
        elif event.key == "r":
            self.action_revert_session()
            event.prevent_default()
        else:
            super().on_key(event)
