import re
import time
from textual.widgets import Input
from textual.binding import Binding
from textual.message import Message
from textual import events

from ..logging import get_logger

logger = get_logger("input")
logger.propagate = False  # prevent double-logging to console panel

ESC_DOUBLE_TAP_MS = 500
PASTE_LINE_THRESHOLD = 3
PASTE_LARGE_CHAR_LIMIT = 20_000
_PASTE_TAG_RE = re.compile(r"\[Pasted ~\d+ lines\]\s*")


class HistoryInput(Input):
    BINDINGS = [
        Binding("up", "history_up", "Previous Command", show=False),
        Binding("down", "history_down", "Next Command", show=False),
    ]

    class InterruptSignal(Message):
        def __init__(self) -> None:
            super().__init__()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int = -1
        self._last_esc_time: float = 0.0
        self._hidden_paste_payload: str | None = None
        self._resolved_text: str | None = None

    @property
    def history(self):
        return self._history

    @history.setter
    def history(self, value):
        self._history = list(value or [])

    @property
    def history_index(self):
        return self._history_index

    @history_index.setter
    def history_index(self, value):
        self._history_index = value

    async def on_paste(self, event: events.Paste) -> None:
        text = event.text or ""
        lines = text.splitlines()
        line_count = len(lines)

        if line_count <= PASTE_LINE_THRESHOLD:
            collapsed = text.replace("\n", " ").replace("\r", " ")
            event.prevent_default()
            self.value = (
                self.value[: self.cursor_position]
                + collapsed
                + self.value[self.cursor_position :]
            )
            self.cursor_position += len(collapsed)
            logger.debug(f"Small paste ({line_count} lines) collapsed to single line")
            return

        event.prevent_default()
        self._hidden_paste_payload = text
        tag = f"[Pasted ~{line_count} lines] "
        self.value = (
            self.value[: self.cursor_position]
            + tag
            + self.value[self.cursor_position :]
        )
        self.cursor_position += len(tag)
        logger.debug(f"Large paste captured: {line_count} lines, {len(text)} chars")

        if len(text) > PASTE_LARGE_CHAR_LIMIT:
            self.notify(
                "Paste is very large, backend will chunk/prune if necessary",
                severity="warning",
            )

    def resolve_submitted_text(self, raw_value: str) -> str:
        if self._resolved_text is not None:
            resolved = self._resolved_text
            self._resolved_text = None
            return resolved
        return raw_value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if not raw:
            self._hidden_paste_payload = None
            self._resolved_text = None
            self._history_index = -1
            self.value = ""
            return

        resolved = raw
        if self._hidden_paste_payload:
            resolved = _PASTE_TAG_RE.sub(self._hidden_paste_payload, raw, count=1)
            self._hidden_paste_payload = None
        self._resolved_text = resolved

        if resolved:
            self._history.append(resolved)
            if len(self._history) > 100:
                self._history.pop(0)
        self._history_index = -1
        self.value = ""

    def on_key(self, event) -> None:
        if event.key == "escape":
            now = time.monotonic()
            if (now - self._last_esc_time) < (ESC_DOUBLE_TAP_MS / 1000.0):
                logger.debug("Double-Esc interrupt triggered")
                self.post_message(self.InterruptSignal())
                self._last_esc_time = 0.0
                event.prevent_default()
                event.stop()
                return
            self._last_esc_time = now
            event.prevent_default()
            event.stop()
            return

    def action_history_up(self) -> None:
        if not self._history:
            return
        if self._history_index == -1:
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self.value = self._history[self._history_index]
        self.cursor_position = len(self.value)

    def action_history_down(self) -> None:
        if self._history_index == -1:
            return
        if self._history_index >= len(self._history) - 1:
            self._history_index = -1
            self.value = ""
            return
        self._history_index += 1
        self.value = self._history[self._history_index]
        self.cursor_position = len(self.value)
