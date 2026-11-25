from textual.widgets import Static
from rich.text import Text
import threading

from ..terminal_sanitize import strip_terminal_controls


class StreamView(Static):
    def __init__(self, role: str = "Agent", **kwargs):
        super().__init__("", **kwargs)
        self._role = role
        self._raw = ""
        self._flush_pending = False
        self._flush_lock = threading.Lock()

    def on_mount(self) -> None:
        with self._flush_lock:
            has_pending = bool(self._raw) or self._flush_pending
        if has_pending:
            self._flush()

    def append_chunk(self, chunk: str) -> None:
        clean = strip_terminal_controls(chunk)
        with self._flush_lock:
            self._raw += clean
            if not self._flush_pending and self.is_mounted:
                self._flush_pending = True
                try:
                    self.call_later(self._flush)
                except Exception:
                    self._flush_pending = False

    def _flush(self) -> None:
        with self._flush_lock:
            self._flush_pending = False
            raw_snapshot = self._raw
        t = Text()
        t.append(f"{self._role}: ", style="bold #10b981")
        t.append(raw_snapshot)
        self.update(t)

    def finalize(self) -> str:
        with self._flush_lock:
            return self._raw
