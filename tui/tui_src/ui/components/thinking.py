from typing import Optional
import time
from textual.app import ComposeResult
from textual.widgets import Static, Label
from textual.containers import Vertical
from textual import on, events
from rich.markdown import Markdown


class ThinkingProcess(Vertical):
    def __init__(self, content: str, start_time: Optional[float] = None):
        super().__init__()
        self.end_time = time.monotonic()
        self.elapsed = self.end_time - (start_time or self.end_time)
        clean_text = str(content) if not isinstance(content, str) else content.replace("Thinking:", "").strip()
        self.raw_content = clean_text
        self.is_expanded = False

    def compose(self) -> ComposeResult:
        time_str = f"{self.elapsed:.2f}s"
        content_str = self.raw_content if isinstance(self.raw_content, str) else str(self.raw_content)
        summary = content_str[:80].replace("\n", " ") + "..."
        yield Label(f"  Thinking: {summary} [ {time_str} ]", classes="header")
        try:
            md = Markdown(content_str)
        except Exception:
            safe = "".join(ch for ch in content_str if ord(ch) >= 32 or ch in "\n\t")
            try:
                md = Markdown(safe)
            except Exception:
                md = content_str
        yield Static(md, classes="content")

    def on_mount(self) -> None:
        self.add_class("pulse")
        self.set_timer(0.5, lambda: self.remove_class("pulse"))

    @on(events.Click)
    def _toggle_expansion(self) -> None:
        self.is_expanded = not self.is_expanded
        self.toggle_class("expanded")
        header = self.query_one(".header", Label)
        time_str = f"{self.elapsed:.2f}s"
        if self.is_expanded:
            header.update(f"  Reasoning Chain [ {time_str} ]")
        else:
            summary = self.raw_content[:80].replace("\n", " ") + "..."
            header.update(f"  Thinking: {summary} [ {time_str} ]")
