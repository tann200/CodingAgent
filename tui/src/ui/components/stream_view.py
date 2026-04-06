from textual.widgets import Static
from textual.reactive import reactive
from rich.text import Text


class StreamView(Static):
    _buffer = reactive("", layout=False, repaint=True)

    def __init__(self, role: str = "Agent", **kwargs):
        super().__init__("", **kwargs)
        self._role = role
        self._raw = ""

    def append_chunk(self, chunk: str) -> None:
        self._raw += chunk
        self._buffer = self._raw

    def watch__buffer(self, new_val: str) -> None:
        t = Text()
        t.append(f"{self._role}: ", style="bold #10b981")
        t.append(new_val)
        self.update(t)

    def finalize(self) -> str:
        result = self._raw
        return result
