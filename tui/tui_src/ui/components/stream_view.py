"""StreamView — native Textual 8.1.0 MarkdownStream widget (TASK-38)."""

from __future__ import annotations

import threading
from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, Markdown

from ..terminal_sanitize import strip_terminal_controls


class StreamView(Vertical):
    """Streams assistant Markdown incrementally via ``Markdown.get_stream()``."""

    DEFAULT_CSS = """
    StreamView {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    StreamView > .stream_role_header {
        color: $success;
        text-style: bold;
        height: 1;
    }
    StreamView > .stream_markdown {
        height: auto;
        padding: 0 1;
        background: transparent;
    }
    """

    def __init__(self, role: str = "Agent", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._role = role
        self._raw = ""
        self._flushed_len = 0
        self._flush_pending = False
        self._flush_lock = threading.Lock()
        self._md_stream: Any = None
        self._finalized = False

    def compose(self) -> ComposeResult:
        yield Label(Text(f"{self._role}:", style="bold"), classes="stream_role_header", markup=False)
        yield Markdown("", classes="stream_markdown")

    def on_mount(self) -> None:
        try:
            md_widget = self.query_one(".stream_markdown", Markdown)
            self._md_stream = Markdown.get_stream(md_widget)
        except Exception:
            self._md_stream = None
        with self._flush_lock:
            has_pending = len(self._raw) > self._flushed_len or self._flush_pending
        if has_pending:
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        try:
            self.call_later(self._flush_async)
        except Exception:
            with self._flush_lock:
                self._flush_pending = False

    def append_chunk(self, chunk: str) -> None:
        clean = strip_terminal_controls(chunk)
        if not clean:
            return
        with self._flush_lock:
            self._raw += clean
            if not self._flush_pending and self.is_mounted:
                self._flush_pending = True
                self._schedule_flush()

    async def _flush_async(self) -> None:
        with self._flush_lock:
            self._flush_pending = False
            delta = self._raw[self._flushed_len :]
            self._flushed_len = len(self._raw)
        if not delta or not self.is_mounted:
            return
        if self._md_stream is not None:
            try:
                await self._md_stream.write(delta)
                return
            except Exception:
                pass
        try:
            md_widget = self.query_one(".stream_markdown", Markdown)
            await md_widget.update(self._raw)
        except Exception:
            pass

    def _flush(self) -> None:
        """Backward-compatible sync flush hook (schedules async MarkdownStream write)."""
        if self.is_mounted:
            self._schedule_flush()

    async def stop_stream(self, final_content: Optional[str] = None) -> str:
        """Flush any remaining content (or replace with *final_content*) and stop the stream cleanly.

        REM-05: Never hold ``self._flush_lock`` across ``await`` points — doing so
        can deadlock the Textual event loop when another coroutine or thread
        calls ``append_chunk`` / ``_flush_async``.
        """
        full_replace_content: Optional[str] = None
        md_stream_to_stop: Any = None
        if final_content is not None:
            clean_final = strip_terminal_controls(final_content)
            with self._flush_lock:
                if clean_final and clean_final != self._raw:
                    if clean_final.startswith(self._raw):
                        self._raw = clean_final
                    else:
                        self._raw = clean_final
                        self._flushed_len = len(clean_final)
                        full_replace_content = clean_final
                        md_stream_to_stop = self._md_stream
                        self._md_stream = None

        if full_replace_content is not None:
            if md_stream_to_stop is not None:
                try:
                    await md_stream_to_stop.stop()
                except Exception:
                    pass
            if self.is_mounted:
                try:
                    await self.query_one(".stream_markdown", Markdown).update(full_replace_content)
                except Exception:
                    pass
            self._finalized = True
            with self._flush_lock:
                return self._raw

        await self._flush_async()
        with self._flush_lock:
            md_stream_to_stop = self._md_stream
            self._md_stream = None
        if md_stream_to_stop is not None:
            try:
                await md_stream_to_stop.stop()
            except Exception:
                pass
        self._finalized = True
        with self._flush_lock:
            return self._raw

    def finalize(self) -> str:
        """Return accumulated raw text and schedule asynchronous stream stop."""
        if self._md_stream is not None and self.is_mounted:
            try:
                self.call_later(self.stop_stream)
            except Exception:
                pass
        self._finalized = True
        with self._flush_lock:
            return self._raw
