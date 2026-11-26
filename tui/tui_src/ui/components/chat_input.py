"""
ChatTextArea — multi-line prompt input with history navigation and slash-command autocomplete.

Enter submits; Shift+Enter inserts a real newline.
Up/Down navigates frecency history only when cursor is on first/last line.
Tab cycles slash-command completions when text starts with '/'.
When the command palette or file picker is active, Up/Down/Enter/Tab are routed
to the app via duck-typed attribute access so this widget stays decoupled from app.

No src.core imports — this widget is purely UI.
"""

from __future__ import annotations

import time
from typing import Any

from textual.message import Message
from textual.widgets import TextArea
from textual import events

from ..logging import get_logger

logger = get_logger("chat_input")

ESC_DOUBLE_TAP_MS: int = 500

SLASH_COMMANDS: list[str] = [
    "/help",
    "/clear",
    "/new",
    "/reset",
    "/compact",
    "/undo",
    "/continue",
    "/interrupt",
    "/status",
    "/doctor",
    "/skills",
    "/theme",
    "/advisor",
    "/btw",
    "/queue",
    "/fast",
    "/reasoning",
    "/provider",
    "/model",
    "/settings",
    "/sessions",
    "/timeline",
    "/diff",
    "/revert",
    "/fork",
    "/rename",
    "/export",
    "/share",
    "/worktree",
    "/mcp",
    "/quit",
]

SLASH_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "/help": "show this list",
    "/clear": "clear chat output",
    "/new": "start a new session",
    "/reset": "reset session (alias for /new)",
    "/compact": "compact conversation context",
    "/undo": "undo last user message (removes from history)",
    "/continue": "restore & re-run previous task",
    "/interrupt": "cancel running agent",
    "/status": "show agent/provider/model status",
    "/doctor": "run local environment & provider health checks",
    "/skills": "list available workspace and bundled skills",
    "/fast": "switch to fastest/smallest model (NANO tier)",
    "/reasoning": "set thinking/reasoning budget (off|low|medium|high|unset|status)",
    "/provider": "list or switch provider",
    "/model": "list or switch model",
    "/settings": "open settings screen",
    "/sessions": "browse saved sessions",
    "/timeline": "view session message timeline",
    "/diff": "show working-directory diff since last snapshot",
    "/revert": "revert working directory to previous snapshot",
    "/fork": "fork current session to a new independent copy",
    "/rename": "rename the current session",
    "/export": "export current session to portable JSONL",
    "/share": "export a redacted shareable session transcript",
    "/worktree": "manage isolated git worktrees for tasks",
    "/mcp": "manage MCP servers: list | add <name> <cmd…> | status",
    "/quit": "exit the application",
}


class ChatTextArea(TextArea):
    """Multi-line prompt input with history navigation and slash-command autocomplete.

    Enter submits the prompt; Shift+Enter inserts a real newline.
    Up/Down arrow navigates history only when the cursor is on the first/last line.
    Tab cycles through slash-command completions when the text starts with '/'.
    When the command palette is visible (app._palette_active), Up/Down/Enter/Tab
    are routed to the palette instead.
    When the file picker is visible (app._at_picker_active), Up/Down/Enter/Tab
    are routed to the file picker instead.
    """

    class Submitted(Message):
        """Posted when the user presses Enter to submit the prompt."""

        def __init__(self, text: str, text_area: "ChatTextArea") -> None:
            super().__init__()
            self.text = text
            self.text_area = text_area

    class TextChanged(Message):
        """Posted on every text change (used to drive command palette and file picker)."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class InterruptSignal(Message):
        """Posted when the user double-taps Escape to interrupt the running agent."""

        pass

    def __init__(self, placeholder: str = "", **kwargs: Any) -> None:
        # Disable syntax highlighting and line numbers — plain prompt input
        kwargs.setdefault("language", None)
        kwargs.setdefault("show_line_numbers", False)
        if placeholder:
            kwargs.setdefault("placeholder", placeholder)
        super().__init__(**kwargs)
        self._prompt_history: list[str] = []
        self.history_index: int = -1
        self._tab_matches: list[str] = []
        self._tab_index: int = -1
        self._placeholder = placeholder
        self._last_esc_time: float = 0.0

    # ── Relay TextArea.Changed so the app can update palette / picker ─────

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Relay built-in TextArea.Changed as our own TextChanged message."""
        self.post_message(self.TextChanged(text=self.text))

    # ── Key routing ───────────────────────────────────────────────────────

    def on_key(self, event: events.Key) -> None:  # type: ignore[override]
        key = event.key
        palette_active: bool = bool(getattr(self.app, "_palette_active", False))
        at_picker_active: bool = bool(getattr(self.app, "_at_picker_active", False))

        # ── Escape: dismiss file picker first, then palette ───────────────
        if key == "escape" and at_picker_active:
            _hide = getattr(self.app, "_at_picker_hide", None)
            if callable(_hide):
                _hide()
            event.prevent_default()
            return

        # ── File picker navigation ────────────────────────────────────────
        if at_picker_active:
            if key in ("up", "down"):
                _nav = getattr(self.app, "_at_picker_navigate", None)
                if callable(_nav):
                    _nav(key)
                event.prevent_default()
                return
            if key in ("enter", "tab"):
                _complete = getattr(self.app, "_at_picker_complete", None)
                if callable(_complete):
                    _complete()
                event.prevent_default()
                return

        # ── Escape: dismiss palette if active ─────────────────────────────
        if key == "escape" and palette_active:
            self.post_message(self.TextChanged(text=""))  # signal hide
            event.prevent_default()
            return

        # ── Escape: double-Esc interrupt guard when no overlay active ─────
        if key == "escape":
            now = time.monotonic()
            if (now - self._last_esc_time) < (ESC_DOUBLE_TAP_MS / 1000.0):
                self._last_esc_time = 0.0
                self.post_message(self.InterruptSignal())
            else:
                self._last_esc_time = now
            event.prevent_default()
            return

        # ── Palette navigation ────────────────────────────────────────────
        if palette_active:
            if key in ("up", "down"):
                _nav = getattr(self.app, "_palette_navigate", None)
                if callable(_nav):
                    _nav(key)
                event.prevent_default()
                return
            if key == "tab":
                _complete = getattr(self.app, "_palette_complete", None)
                completed: str = str(_complete()) if callable(_complete) else ""
                if completed:
                    self.load_text(completed)
                    self.move_cursor(self.document.end)
                    self.post_message(self.TextChanged(text=completed))
                event.prevent_default()
                return
            if key == "enter":
                # TASK-37: If text is already a complete slash command or has args,
                # submit immediately instead of trapping Enter in a completion loop.
                raw_stripped = self.text.strip()
                if raw_stripped in SLASH_COMMANDS or " " in raw_stripped:
                    _complete = getattr(self.app, "_palette_complete", None)
                    if callable(_complete):
                        _complete()
                    self.post_message(self.Submitted(text=self.text.rstrip("\n"), text_area=self))
                    event.prevent_default()
                    return
                _complete = getattr(self.app, "_palette_complete", None)
                completed_enter: str = str(_complete()) if callable(_complete) else ""
                if completed_enter:
                    self.load_text(completed_enter)
                    self.move_cursor(self.document.end)
                event.prevent_default()
                return

        # ── Enter → submit ────────────────────────────────────────────────
        if key == "enter":
            text = self.text.rstrip("\n")
            if text.strip():
                self.post_message(self.Submitted(text=text, text_area=self))
            event.prevent_default()
            return

        # ── Shift+Enter → literal newline ─────────────────────────────────
        if key == "shift+enter":
            self.insert("\n")
            event.prevent_default()
            return

        # ── Tab → slash-command autocomplete cycle ────────────────────────
        if key == "tab":
            current = self.text.rstrip("\n")
            if current.startswith("/"):
                matches = [c for c in SLASH_COMMANDS if c.startswith(current)]
                if matches:
                    if self._tab_matches != matches:
                        self._tab_matches = matches
                        self._tab_index = 0
                    else:
                        self._tab_index = (self._tab_index + 1) % len(matches)
                    self.load_text(self._tab_matches[self._tab_index])
                    self.move_cursor(self.document.end)
                event.prevent_default()
                return
            self._tab_matches = []
            self._tab_index = -1
            return

        # ── Reset tab state on any other key ─────────────────────────────
        if key != "shift+tab":
            self._tab_matches = []
            self._tab_index = -1

        # ── Up → history (descending frecency: index 0 = most frequent/recent)
        if key == "up" and self.cursor_at_first_line and not palette_active:
            if (
                self._prompt_history
                and self.history_index < len(self._prompt_history) - 1
            ):
                self.history_index += 1
                self.load_text(self._prompt_history[self.history_index])
                self.move_cursor(self.document.end)
                event.prevent_default()
            return

        # ── Down → history (back toward most frequent/recent, then clear) ─
        if key == "down" and self.cursor_at_last_line and not palette_active:
            if self.history_index > 0:
                self.history_index -= 1
                self.load_text(self._prompt_history[self.history_index])
                self.move_cursor(self.document.end)
                event.prevent_default()
            elif self.history_index == 0:
                self.history_index = -1
                self.clear()
                event.prevent_default()
            return
