from textual.widgets import Static
from rich.text import Text


class StreamView(Static):
    def __init__(self, role: str = "Agent", **kwargs):
        super().__init__("", **kwargs)
        self._role = role
        self._raw = ""
        self._flush_pending = False

    def append_chunk(self, chunk: str) -> None:
        self._raw += chunk
        if not self._flush_pending:
            self._flush_pending = True
            self.call_later(self._flush)

    def _flush(self) -> None:
        self._flush_pending = False
        t = Text()
        t.append(f"{self._role}: ", style="bold #10b981")
        t.append(self._raw)
        self.update(t)

    def finalize(self) -> str:
        return self._raw
