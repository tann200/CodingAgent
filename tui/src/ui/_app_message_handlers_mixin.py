"""AppMessageHandlersMixin — streaming, message display, and chat-input handlers.

Extracted from ``tui/src/ui/app.py`` (lines 866–1001, 1826–1911) to reduce
AgentApp to a ≤400-line core.
"""

from __future__ import annotations

from textual import on
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Label, Static

from .bus import (
    AgentFinalResponse,
    AgentRunningEvent,
    DisplayReasoning,
    DoomLoopEvent,
    McpServerStatusEvent,
    ModelRoutingEvent,
    StepFinishEvent,
    StepStartEvent,
    StreamingThinkingUpdate,
    StreamChunkEvent,
    ToolPermissionEvent,
    UsageTurnSummaryEvent,
    WorkerError,
)
from .components import ChatTextArea, HistoryInput
from .events import AgentInterrupt, SlashCommand, ToolPermissionApproved
from .logging import get_logger

logger = get_logger("app_msghandlers")


class AppMessageHandlersMixin:
    """Streaming output, agent-running gating, message queuing, and text-input handlers.

    Expects the host class to expose:
    - ``self.agent_running`` (reactive bool)
    - ``self._queued_messages`` (deque[str])
    - ``self._queued_message`` (str | None)
    - ``self._queued_widget`` (Static | None)
    - ``self._bridge`` (AgentBridge)
    - ``self._settings`` (SettingsStore)
    - ``self._last_task_text`` (str)
    - ``self._palette_active``, ``self._palette_matches`` (list)
    - ``self._perm_tool_names`` (dict[str, str])
    - ``self._allow_always_tools`` (set[str])
    - ``self._pending_perm_count`` (int)
    - ``self._update_status_bar``, ``self._update_perm_badge``
    - ``self._at_picker_hide``, ``self._expand_at_tokens``
    - ``self._mount_chat_widget``, ``self._prune_chat_log``
    - ``self._chat_handle_stream_chunk``, ``self._chat_handle_thinking_update``
    - ``self._chat_handle_reasoning``, ``self._chat_handle_final_response``
    - ``self._chat_handle_error``, ``self._chat_handle_usage_turn_summary``
    - ``self._chat_handle_doom_loop``, ``self._chat_handle_session_health``
    - ``self._chat_handle_text_changed``
    - ``self._finalize_stream``
    - ``self._update_mcp_status_chip`` — from StatusBarMixin
    - ``self.query_one``, ``self.call_later``, ``self.notify``, ``self.post_message``
    """

    # ── Agent running gate ────────────────────────────────────────────────

    @on(AgentRunningEvent)
    def handle_agent_running(self, event) -> None:
        self.agent_running = event.running
        self._update_status_bar()
        try:
            inp = self.query_one("#user_input")
            if event.running:
                inp.add_class("input_locked")
            else:
                inp.remove_class("input_locked")
        except Exception:
            pass

        if not event.running and self._queued_messages:
            self.call_later(self._drain_message_queue)

    def _drain_message_queue(self) -> None:
        while self._queued_messages and not self.agent_running:
            msg = self._queued_messages.popleft()
            logger.info(f"Draining queued message: {msg[:60]}")
            self._bridge.send_prompt(msg)

    # ── Model routing ─────────────────────────────────────────────────────

    @on(ModelRoutingEvent)
    def handle_model_routing(self, event) -> None:
        logger.info(f"Model routing: {event.provider} / {event.model}")
        try:
            self.query_one("#sb_provider", Static).update(event.provider)
            self.query_one("#sb_model_info", Static).update(event.model)
            banner = self.query_one("#provider_banner", Static)
            banner.update(f"  {event.provider}  ·  {event.model}")
            banner.remove_class("connected", "error")
            banner.add_class("connected")
        except Exception:
            pass

    # ── Streaming display — thin delegates to ChatDisplayMixin ────────────

    @on(StreamChunkEvent)
    def handle_stream_chunk(self, event) -> None:
        self._chat_handle_stream_chunk(event)

    @on(StreamingThinkingUpdate)
    def handle_thinking_update(self, event) -> None:
        self._chat_handle_thinking_update(event)

    @on(DisplayReasoning)
    async def handle_reasoning(self, event) -> None:
        await self._chat_handle_reasoning(event)

    @on(AgentFinalResponse)
    async def handle_final_response(self, event) -> None:
        await self._chat_handle_final_response(event)

    @on(WorkerError)
    async def handle_error(self, event) -> None:
        await self._chat_handle_error(event)

    # ── Step boundary events ──────────────────────────────────────────────

    @on(StepStartEvent)
    def handle_step_start(self, event) -> None:
        label = f"⟳ {event.tool}"
        if event.step and event.total:
            label = f"⟳ {event.tool} [{event.step}/{event.total}]"
        try:
            self.query_one("#sb_tool_activity", Static).update(label)
        except Exception:
            pass

    @on(StepFinishEvent)
    def handle_step_finish(self, event) -> None:
        icon = "✓" if event.ok else "✗"
        elapsed = f" {event.elapsed_ms}ms" if event.elapsed_ms is not None else ""
        label = f"{icon} {event.tool}{elapsed}"
        try:
            self.query_one("#sb_tool_activity", Static).update(label)
        except Exception:
            pass

    # ── MCP server status chip ─────────────────────────────────────────────

    @on(McpServerStatusEvent)
    def handle_mcp_status(self, event) -> None:
        self._update_mcp_status_chip(event.running, event.count, event.has_error)

    # ── Tool permission gate ───────────────────────────────────────────────

    @on(ToolPermissionEvent)
    async def handle_tool_permission(self, event) -> None:
        logger.warning(f"Tool permission required: {event.tool}  id={event.tool_id}")
        if not hasattr(self, "_perm_tool_names"):
            self._perm_tool_names: dict[str, str] = {}
        self._perm_tool_names[event.tool_id] = event.tool
        if event.tool in self._allow_always_tools:
            logger.info(f"Auto-approving {event.tool} (allow-always)")
            self._bridge.publish("tool.permission_granted", {"tool_id": event.tool_id})
            self.post_message(ToolPermissionApproved(tool_id=event.tool_id))
            return
        self._update_perm_badge(+1)
        from ._app_tool_handlers_mixin import _fmt_args

        args_fmt = _fmt_args(event.args)
        chat_log = self.query_one("#chat_log", VerticalScroll)
        warn = Static(
            f"[bold #facc15]⚠ Permission required:[/] [bold]{event.tool}[/]  {args_fmt}",
            classes="retry_msg",
            markup=True,
        )
        await chat_log.mount(warn)
        tid = event.tool_id
        row = Horizontal(id=f"tool_perm_{tid}", classes="approval_row")
        await chat_log.mount(row)
        await row.mount(
            Button("Allow", id=f"btn_tool_perm_allow_{tid}", variant="success")
        )
        await row.mount(Button("Deny", id=f"btn_tool_perm_deny_{tid}", variant="error"))
        await row.mount(
            Button("Allow Always", id=f"btn_tool_perm_always_{tid}", variant="warning")
        )
        chat_log.scroll_end(animate=False)
        self._prune_chat_log()

    # ── Per-turn usage summary / doom loop ────────────────────────────────

    @on(UsageTurnSummaryEvent)
    def handle_usage_turn_summary(self, event) -> None:
        self._chat_handle_usage_turn_summary(event)

    @on(DoomLoopEvent)
    async def handle_doom_loop(self, event) -> None:
        await self._chat_handle_doom_loop(event)

    # ── Input handling: ChatTextArea ──────────────────────────────────────

    @on(ChatTextArea.Submitted)
    async def on_chat_text_area_submitted(self, event) -> None:
        raw_val = event.text.strip()
        if not raw_val:
            return

        event.text_area.clear()
        event.text_area.history_index = -1
        self._palette_active = False
        self._palette_matches = []
        self._at_picker_hide()

        if raw_val.startswith("/"):
            parts = raw_val[1:].split(" ", 1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            self.post_message(SlashCommand(command=cmd, args=args))
            return

        if self.agent_running:
            display_val_q = (
                raw_val
                if len(raw_val) <= 200
                else raw_val[:200] + f"… ({len(raw_val)} chars)"
            )
            q_widget = Static(
                f"[bold #3b82f6]You:[/] {display_val_q}  [bold #a78bfa reverse] INJECTING [/]",
                classes="user_msg",
                markup=True,
            )
            self.call_later(self._mount_chat_widget, q_widget)
            self._bridge.send_prompt(raw_val)
            return

        val = self._expand_at_tokens(raw_val)

        display_val = val if len(val) <= 200 else val[:200] + f"… ({len(val)} chars)"
        logger.info(f"User prompt: {val[:80]}")

        self._bridge.update_prompt_history(raw_val)
        try:
            inp = self.query_one("#user_input")
            inp._prompt_history = self._bridge.load_prompt_history()
        except Exception:
            pass

        task_text = val[:80] + ("…" if len(val) > 80 else "")
        try:
            self.query_one("#sb_task_status", Static).update(task_text)
        except Exception:
            pass

        widget = Label(
            f"[bold #3b82f6]You:[/] {display_val}", classes="user_msg", markup=True
        )
        await self._mount_chat_widget(widget)

        self._last_task_text = val
        sent = self._bridge.send_prompt(val)
        if not sent:
            self.notify("Agent already running", severity="warning")

    @on(ChatTextArea.TextChanged)
    def on_chat_text_area_changed(self, event) -> None:
        self._chat_handle_text_changed(event)

    # ── Interrupt signal from HistoryInput (kept for compat) ──────────────

    @on(HistoryInput.InterruptSignal)
    def handle_interrupt(self, event) -> None:
        logger.warning("Double-Esc interrupt signal received")
        self._bridge.force_interrupt()
        self._finalize_stream()
        self.notify("Agent interrupted", severity="warning")
        self.post_message(AgentInterrupt())
