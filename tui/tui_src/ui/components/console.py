import threading

from textual.widgets import Static, RichLog
from textual.containers import Vertical
from textual.app import ComposeResult
from textual.reactive import reactive
from rich.text import Text

from ..logging import get_memory_handler, get_logger

logger = get_logger("console")

LEVEL_STYLES = {
    "DEBUG": "#666666",
    "INFO": "#3b82f6",
    "WARNING": "#facc15",
    "ERROR": "#ff5555",
    "CRITICAL": "#ff0000",
}


class ConsolePanel(Vertical):
    line_count = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._handler = get_memory_handler()

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]Console[/bold]  (ctrl+l to toggle)", id="console_header", markup=True
        )
        yield RichLog(
            id="console_output",
            wrap=True,
            auto_scroll=True,
            highlight=True,
            max_lines=500,
        )

    def on_mount(self) -> None:
        log_widget = self.query_one("#console_output", RichLog)
        for line in self._handler.get_lines():
            self._write_styled_line(log_widget, line)
        self._handler.register_callback(self._on_new_log)

    def on_unmount(self) -> None:
        self._handler.unregister_callback(self._on_new_log)

    def _on_new_log(self, line: str) -> None:
        app = getattr(self, "app", None)
        app_thread_id = getattr(app, "_thread_id", None) if app is not None else None
        if app is not None and app_thread_id is not None and threading.get_ident() != app_thread_id:
            # Prefer non-blocking event-loop scheduling so logging during UI teardown
            # or thread joins cannot deadlock call_from_thread.
            loop = getattr(app, "_loop", None)
            if loop is not None and hasattr(loop, "call_soon_threadsafe"):
                try:
                    if not getattr(loop, "is_closed", lambda: False)():
                        loop.call_soon_threadsafe(self._append_log_line, line)
                except Exception:
                    pass
                return
            try:
                app.call_from_thread(self._append_log_line, line)
            except Exception:
                pass
            return
        self._append_log_line(line)

    def _append_log_line(self, line: str) -> None:
        try:
            log_widget = self.query_one("#console_output", RichLog)
            self._write_styled_line(log_widget, line)
            self.line_count += 1
        except Exception:
            pass

    def _write_styled_line(self, log_widget: RichLog, line: str) -> None:
        color = "#888888"
        for level, style in LEVEL_STYLES.items():
            if f"[{level}" in line:
                color = style
                break
        t = Text(line, style=color)
        log_widget.write(t)

    def write_line(self, line: str, level: str = "INFO") -> None:
        """Write DIRECTLY to RichLog — bypasses logging system (§16.4 compliance)."""
        try:
            log_widget = self.query_one("#console_output", RichLog)
            color = LEVEL_STYLES.get(level.upper(), "#888888")
            t = Text(line, style=color)
            log_widget.write(t)
            self.line_count += 1
        except Exception:
            pass

    def append_line(self, line: str, level: str = "INFO") -> None:
        logger.log(
            {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}.get(
                level, 20
            ),
            line,
        )
