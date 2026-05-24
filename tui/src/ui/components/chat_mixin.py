"""ChatDisplayMixin — extracted chat/stream display helpers (P1-6 Phase B).

Extracted from ``tui/src/ui/app.py`` to reduce its line count toward the ≤400 line
target.  Contains:

- Stream widget lifecycle helpers (_ensure_stream_widget, _mount_and_scroll,
  _finalize_stream, _prune_chat_log, _mount_chat_widget, _sched_chat_widget,
  _clear_chat_panel)
- @file picker helpers (_list_workspace_files, _at_picker_navigate,
  _at_picker_complete, _at_picker_hide)
- Inline slash palette helpers (_palette_navigate, _palette_complete)
- @token expansion (_expand_at_tokens)
- Chat-display event handler implementations (_chat_handle_* methods)
  — called from thin @on-decorated stubs in AgentApp

``AgentApp`` must inherit this mixin and expose the following attributes:

Reactive / scalar:
  is_streaming: bool
  active_role: str
  agent_running: bool (read-only in these methods)

Instance dicts / lists:
  _current_stream: StreamView | None
  _tool_widgets: dict[str, Widget]
  _tool_args: dict[str, dict]
  _at_file_cache: list[str]
  _at_file_cache_ts: float
  _at_picker_active: bool
  _at_picker_matches: list[str]
  _at_picker_index: int
  _at_picker_widget: Widget | None
  _at_prefix: str
  _palette_active: bool
  _palette_matches: list[str]
  _palette_index: int

Textual / other:
  _bridge: AgentBridge   (read-only: .working_dir)
  query_one(selector, type)
  call_later(fn, *args)
  notify(msg, severity=...)

Also calls:
  _update_status_bar()  — from StatusBarMixin (resolved at runtime via self)
  _prune_chat_log()     — defined in this mixin
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Label, Static

from ..logging import get_logger
from .status_bar import ROLE_LABELS, ROLE_COLORS

if TYPE_CHECKING:
    from ..components import StreamView  # noqa: F401 (type hint only)

logger = get_logger("chat_mixin")

# Duplicated from app.py module-level constants so this module is self-contained
_AT_FILE_MAX_CHARS: int = 8_000
_MAX_CHAT_WIDGETS: int = 200
_AT_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}
)


class ChatDisplayMixin:
    """Mixin owning chat-log rendering and stream lifecycle helpers.

    All methods use only ``self.query_one()``, ``self._current_stream``,
    ``self.is_streaming``, ``self.active_role``, ``self._tool_widgets``,
    ``self._tool_args``, ``self._bridge`` (read-only), and the picker/palette
    state attributes listed in the module docstring.  No circular imports.
    """

    # ==================================================================
    # Stream widget lifecycle
    # ==================================================================

    def _ensure_stream_widget(self):
        """Return the active StreamView, creating one if needed."""
        from tui.src.ui.components.stream_view import StreamView  # type: ignore[import]

        if self._current_stream is None:
            self.is_streaming = True
            role = ROLE_LABELS.get(self.active_role, self.active_role)
            self._current_stream = StreamView(role=role, classes="stream_msg")
            chat_log = self.query_one("#chat_log", VerticalScroll)
            self.call_later(self._mount_and_scroll, self._current_stream, chat_log)
        return self._current_stream

    async def _mount_and_scroll(self, widget, container) -> None:
        """Async-mount *widget* into *container* then scroll to the end."""
        await container.mount(widget)
        container.scroll_end(animate=False)

    def _finalize_stream(self) -> None:
        """Tear down the current stream widget and update the status bar."""
        if self._current_stream is not None:
            self._current_stream = None
            self.is_streaming = False
            self._update_status_bar()  # provided by StatusBarMixin at runtime

    def _prune_chat_log(self) -> None:
        """Remove oldest widgets from chat_log when it exceeds _MAX_CHAT_WIDGETS."""
        try:
            chat_log = self.query_one("#chat_log", VerticalScroll)
            while len(chat_log.children) > _MAX_CHAT_WIDGETS:
                chat_log.children[0].remove()
        except Exception:
            pass

    async def _mount_chat_widget(self, widget) -> None:
        """Async-mount *widget* into the chat log and scroll to the bottom."""
        chat_log = self.query_one("#chat_log", VerticalScroll)
        await chat_log.mount(widget)
        chat_log.scroll_end(animate=False)
        self._prune_chat_log()

    def _sched_chat_widget(self, widget) -> None:
        """Schedule a widget mount in the chat log from a sync handler."""
        chat_log = self.query_one("#chat_log", VerticalScroll)
        self.call_later(self._mount_and_scroll, widget, chat_log)
        self.call_later(self._prune_chat_log)

    def _clear_chat_panel(self) -> None:
        """Clear the stream, tool widget caches, and all chat log children."""
        self._finalize_stream()
        self._tool_widgets.clear()
        self._tool_args.clear()
        try:
            self.query_one("#chat_log", VerticalScroll).remove_children()
        except Exception:
            pass

    # ==================================================================
    # @file picker helpers
    # ==================================================================

    def _list_workspace_files(
        self, query: str = "", max_results: int = 30
    ) -> list[str]:
        """Return workspace file paths matching *query*, relative to working_dir.

        Results are cached for 60 s to avoid rescanning on every keystroke.
        """
        now = time.monotonic()
        if not self._at_file_cache or now - self._at_file_cache_ts > 60.0:
            try:
                wd = Path(self._bridge.working_dir or os.getcwd())
                all_files: list[str] = []
                for root, dirs, fnames in os.walk(str(wd)):
                    dirs[:] = [d for d in dirs if d not in _AT_SKIP_DIRS]
                    for fname in fnames:
                        all_files.append(
                            os.path.relpath(os.path.join(root, fname), str(wd))
                        )
                        if len(all_files) >= 2000:
                            break
                    if len(all_files) >= 2000:
                        break
                self._at_file_cache = sorted(all_files)
                self._at_file_cache_ts = now
            except Exception:
                return []
        if not query:
            return self._at_file_cache[:max_results]
        q = query.lower()
        return [f for f in self._at_file_cache if q in f.lower()][:max_results]

    def _at_picker_navigate(self, direction: str) -> None:
        """Move the file picker selection up or down."""
        if not self._at_picker_matches:
            return
        if direction == "up":
            self._at_picker_index = max(0, self._at_picker_index - 1)
        else:
            self._at_picker_index = min(
                len(self._at_picker_matches) - 1, self._at_picker_index + 1
            )
        if self._at_picker_widget is not None:
            self._at_picker_widget.update_picker(
                self._at_picker_matches, self._at_picker_index
            )

    def _at_picker_complete(self) -> None:
        """Replace the @token in ChatTextArea with the selected file path."""
        from tui.src.ui.components.chat_input import ChatTextArea  # type: ignore[import]

        if not self._at_picker_matches or not (
            0 <= self._at_picker_index < len(self._at_picker_matches)
        ):
            return
        chosen = self._at_picker_matches[self._at_picker_index]
        try:
            ta = self.query_one("#user_input", ChatTextArea)
            current = ta.text
            new_text = re.sub(r"@\S*$", f"@{chosen} ", current)
            ta.load_text(new_text)
            ta.move_cursor(ta.document.end)
        except Exception:
            pass
        self._at_picker_hide()

    def _at_picker_hide(self) -> None:
        """Hide the file picker without completing."""
        self._at_picker_active = False
        self._at_picker_matches = []
        self._at_prefix = ""
        if self._at_picker_widget is not None:
            self._at_picker_widget.display = False

    # ==================================================================
    # Inline slash palette helpers
    # ==================================================================

    def _palette_navigate(self, direction: str) -> None:
        """Move the palette selection up or down."""
        if not self._palette_matches:
            return
        if direction == "up":
            self._palette_index = max(0, self._palette_index - 1)
        else:
            self._palette_index = min(
                len(self._palette_matches) - 1, self._palette_index + 1
            )

    def _palette_complete(self) -> str:
        """Return the currently-selected command and hide the inline palette."""
        if self._palette_matches and 0 <= self._palette_index < len(
            self._palette_matches
        ):
            cmd = self._palette_matches[self._palette_index]
        else:
            cmd = ""
        self._palette_active = False
        self._palette_index = 0
        self._palette_matches = []
        return cmd

    # ==================================================================
    # @token expansion
    # ==================================================================

    def _expand_at_tokens(self, text: str) -> str:
        """Replace '@path' tokens with file content wrapped in XML tags.

        Only expands tokens that resolve to readable files within the workspace.
        Leaves bare '@word' tokens that don't match files unchanged.
        """
        try:
            wd = Path(self._bridge.working_dir or os.getcwd()).resolve()
        except Exception:
            return text

        def _replace(m: re.Match) -> str:
            rel = m.group(1)
            try:
                target = (wd / rel).resolve()
                if not str(target).startswith(str(wd)):
                    return m.group(0)
                if target.is_file():
                    content = target.read_text(errors="replace")[:_AT_FILE_MAX_CHARS]
                    return f"<file: {rel}>\n{content}\n</file>"
            except Exception:
                pass
            return m.group(0)

        return re.sub(r"@(\S+)", _replace, text)

    # ==================================================================
    # Chat-display event handler implementations
    # (called from @on-decorated stubs in AgentApp)
    # ==================================================================

    def _chat_handle_stream_chunk(self, event) -> None:
        stream = self._ensure_stream_widget()
        stream.append_chunk(event.chunk)
        if not event.is_partial:
            self._finalize_stream()
        try:
            self.query_one("#chat_log", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def _chat_handle_thinking_update(self, event) -> None:
        stream = self._ensure_stream_widget()
        stream.append_chunk(event.content)
        if event.is_complete:
            self._finalize_stream()

    async def _chat_handle_reasoning(self, event) -> None:
        self._finalize_stream()
        from tui.src.ui.components.thinking import ThinkingProcess  # type: ignore[import]

        widget = ThinkingProcess(event.content, event.start_time)
        await self._mount_chat_widget(widget)

    async def _chat_handle_final_response(self, event) -> None:
        from tui.src.ui.components.artifact import AgentArtifact  # type: ignore[import]

        self._finalize_stream()
        logger.info("Agent final response received")
        artifact = AgentArtifact(
            content=event.content, title="Response", kind="markdown"
        )
        await self._mount_chat_widget(artifact)

    async def _chat_handle_error(self, event) -> None:
        self._finalize_stream()
        logger.error(f"Worker error: {event.message}")
        widget = Static(
            f"[bold red]✗ Error:[/] {event.message}",
            classes="error_msg",
            markup=True,
        )
        await self._mount_chat_widget(widget)

    def _chat_handle_usage_turn_summary(self, event) -> None:
        """Append a dim token/cost footer after the most recent assistant message."""
        parts = []
        if event.input_tokens or event.output_tokens:
            parts.append(f"{event.input_tokens:,} in / {event.output_tokens:,} out")
        if event.cost_usd > 0:
            parts.append(f"${event.cost_usd:.4f}")
        if not parts:
            return
        footer_text = "  ".join(parts)
        widget = Static(
            f"[dim #555555]  ↳ {footer_text}[/]",
            classes="usage_footer",
            markup=True,
        )
        self._sched_chat_widget(widget)

    async def _chat_handle_doom_loop(self, event) -> None:
        """Show a confirmation dialog when the agent is stuck in a repeating loop."""
        logger.warning(
            f"Doom-loop detected: tool={event.tool_name!r}  count={event.count}"
        )
        chat_log = self.query_one("#chat_log", VerticalScroll)
        warn = Static(
            f"[bold #ff5555]⚠ Loop detected:[/] [bold]{event.tool_name}[/] "
            f"called {event.count}× with identical arguments.",
            classes="retry_msg",
            markup=True,
        )
        await chat_log.mount(warn)
        tid = event.tool_id or f"doom_{event.fingerprint[:8]}"
        row = Horizontal(id=f"doom_row_{tid}", classes="approval_row")
        await chat_log.mount(row)
        await row.mount(
            Button("Continue anyway", id=f"btn_doom_allow_{tid}", variant="warning")
        )
        await row.mount(
            Button("Stop agent", id=f"btn_doom_deny_{tid}", variant="error")
        )
        chat_log.scroll_end(animate=False)
        self._prune_chat_log()

    async def _chat_handle_plan_requested(self, event) -> None:
        """Mount plan text + Approve/Reject buttons in the chat log."""
        logger.info("Plan approval requested")
        self._finalize_stream()
        chat_log = self.query_one("#chat_log", VerticalScroll)
        if event.plan_text:
            plan_display = Static(
                f"[bold #a855f7]Plan:[/]\n{event.plan_text}",
                classes="plan_msg",
                markup=True,
            )
            await chat_log.mount(plan_display)
        approval = Horizontal(id="plan_approval", classes="approval_row")
        await chat_log.mount(approval)
        await approval.mount(
            Static("Approve this plan?  ", classes="approval_label", markup=True)
        )
        await approval.mount(
            Button("Approve", id="btn_approve_plan", variant="success")
        )
        await approval.mount(Button("Reject", id="btn_reject_plan", variant="error"))
        chat_log.scroll_end(animate=False)
        self._prune_chat_log()

    async def _chat_handle_session_health(self, event) -> None:
        color_map = {"error": "#ff5555", "warning": "#facc15", "info": "#3b82f6"}
        color = color_map.get(event.level, "#888888")
        widget = Static(
            f"[bold {color}]⚠ {event.title}:[/] {event.message}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(widget)

    async def _chat_handle_context_compacted(self, event) -> None:
        """Insert a visual compaction divider on auto-compaction."""
        divider = Static(
            "[dim]══════════════ Context Compacted ══════════════[/]",
            classes="system_msg compaction_divider",
            markup=True,
        )
        await self._mount_chat_widget(divider)

    async def _chat_handle_context_degraded(self, event) -> None:
        logger.warning(f"Context degraded: {event.reason}")
        self.notify(f"Context degraded: {event.reason}", severity="warning")
        w = Static(
            f"[bold #facc15]⚠ Context degraded:[/] {event.reason}"
            + (
                f"  (target window: {event.target_window:,})"
                if event.target_window
                else ""
            ),
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)

    async def _chat_handle_retry_attempt(self, event) -> None:
        logger.warning(
            f"Retry {event.attempt_number}/{event.max_attempts}: {event.error_type}"
        )
        prov = f"  [{event.provider}]" if event.provider else ""
        w = Static(
            f"[bold #facc15]↻ Retry {event.attempt_number}/{event.max_attempts}:[/] "
            f"{event.error_type}{prov}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)

    async def _chat_handle_retry_succeeded(self, event) -> None:
        logger.info(f"Retry succeeded on attempt {event.attempt_number}")
        prov = f"  [{event.provider}]" if event.provider else ""
        w = Static(
            f"[bold #22c55e]✓ Retry succeeded[/] on attempt {event.attempt_number}{prov}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)

    async def _chat_handle_retry_failed(self, event) -> None:
        logger.error(f"All retries failed: {event.error_type}")
        self.notify("All retry attempts failed", severity="error")
        prov = f"  [{event.provider}]" if event.provider else ""
        w = Static(
            f"[bold #ff5555]✗ All {event.total_attempts} retries failed:[/] "
            f"{event.error_type}{prov}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)

    def _chat_handle_text_changed(self, event) -> None:
        """Drive inline palette and @file picker from every text change."""
        from tui.src.ui.components.chat_input import SLASH_COMMANDS  # type: ignore[import]

        text = event.text
        first_line = text.split("\n", 1)[0]
        if first_line.startswith("/"):
            matches = [c for c in SLASH_COMMANDS if c.startswith(first_line.rstrip())]
            if matches:
                self._palette_active = True
                self._palette_matches = matches
                self._palette_index = 0
                return
        if self._palette_active:
            self._palette_active = False
            self._palette_matches = []

        at_match = re.search(r"@(\S*)$", text)
        if at_match:
            query = at_match.group(1)
            self._at_prefix = "@" + query
            matches_files = self._list_workspace_files(query)
            if matches_files:
                self._at_picker_matches = matches_files
                self._at_picker_index = 0
                self._at_picker_active = True
                if self._at_picker_widget is not None:
                    self._at_picker_widget.update_picker(matches_files, 0)
                return
        self._at_picker_hide()
