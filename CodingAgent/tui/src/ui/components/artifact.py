from textual.widgets import Static
from textual.reactive import reactive
from rich.syntax import Syntax
from rich.markdown import Markdown
from rich.panel import Panel
from rich.console import RenderableType
from rich.text import Text
import logging

_log = logging.getLogger(__name__)


class AgentArtifact(Static):
    _stream_content: reactive[str] = reactive("", layout=False, repaint=True)

    def __init__(
        self, content: str = "", title: str = "Artifact", kind: str = "markdown"
    ):
        super().__init__("")
        self.title = title
        self.kind = kind
        self._content_str = content

    def on_mount(self) -> None:
        self._stream_content = self._content_str

    def watch__stream_content(self, new_content: str) -> None:
        try:
            self.update(self._build_renderable(new_content))
        except Exception as _err:
            _log.warning("artifact render error (kind=%s): %s", self.kind, _err)

    def append_chunk(self, chunk: str) -> None:
        self._content_str += chunk
        self._stream_content = self._content_str

    def _sanitize(self, text: str) -> str:
        return "".join(ch for ch in text if ord(ch) >= 32 or ch in "\n\t")

    def _build_renderable(self, content_str: str) -> RenderableType:
        if not content_str:
            return Text("")

        if self.kind == "diff":
            syntax = Syntax(
                content_str, "diff", theme="monokai", line_numbers=True, word_wrap=True
            )
            return Panel(
                syntax, title=f"  {self.title}", border_style="green", expand=True
            )

        if self.kind == "markdown":
            try:
                md = Markdown(content_str)
                return Panel(
                    md, title=f"  {self.title}", border_style="blue", expand=True
                )
            except Exception:
                safe = self._sanitize(content_str)
                try:
                    md = Markdown(safe)
                    return Panel(
                        md, title=f"  {self.title}", border_style="blue", expand=True
                    )
                except Exception:
                    return Panel(
                        Text(safe),
                        title=f"  {self.title} (plain)",
                        border_style="blue",
                        expand=True,
                    )

        return Text(content_str)
