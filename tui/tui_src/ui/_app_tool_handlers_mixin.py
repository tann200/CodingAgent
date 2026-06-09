"""AppToolHandlersMixin — tool-call, subagent, diff, plan, and approval-button handlers.

Extracted from ``tui/src/ui/app.py`` (lines 1006–1609) to reduce AgentApp
to a ≤400-line core.
"""

from __future__ import annotations

from textual import on
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Static
from textual.widget import Widget

from .bus import (
    BashApprovalEvent,
    DiffPreviewEvent,
    PlanProgressEvent,
    PlanRequestedEvent,
    SubagentFinishEvent,
    SubagentStartEvent,
    ToolCallErrorEvent,
    ToolCallFinishEvent,
    ToolCallStartEvent,
    ToolExecutionNotice,
)
from .components import SideBySideDiff, SubagentProgress
from .events import (
    BashApproved,
    BashDenied,
    PlanApproved,
    PlanRejected,
    ToolPermissionApproved,
    ToolPermissionDenied,
)
from .logging import get_logger

from ._app_protocol import AgentAppProtocol

logger = get_logger("tool_handlers")

# ── Per-tool icons matching OpenCode's InlineTool icons.
_TOOL_ICONS: dict[str, str] = {
    "read_file": "→",
    "read": "→",
    "write_file": "←",
    "write": "←",
    "edit_file": "←",
    "edit": "←",
    "apply_patch": "%",
    "bash": "#",
    "run_bash": "#",
    "glob": "✱",
    "grep": "✱",
    "list_files": "→",
    "ls": "→",
    "list": "→",
    "webfetch": "%",
    "websearch": "◈",
    "codesearch": "◇",
    "task": "│",
    "delegation": "│",
    "todowrite": "⚙",
    "question": "→",
    "skill": "→",
}

_TODO_STATUS_ICONS: dict[str, str] = {
    "pending": "○",
    "in_progress": "●",
    "completed": "✓",
    "cancelled": "✗",
    "done": "✓",
}

_TODO_STATUS_COLORS: dict[str, str] = {
    "pending": "#888888",
    "in_progress": "#facc15",
    "completed": "#22c55e",
    "cancelled": "#ff5555",
    "done": "#22c55e",
}

def _fmt_args(tool_args: dict) -> str:
    """Format tool args per §6.2: truncate >120 chars; omit content/patch."""
    parts = []
    for k, v in list(tool_args.items())[:5]:
        if k in ("content", "patch", "new_string", "old_string"):
            parts.append(f"{k}=<{len(str(v))} chars>")
        else:
            sv = str(v)
            if len(sv) > 120:
                sv = sv[:117] + "…"
            parts.append(f'{k}="{sv}"')
    return "  ".join(parts)

def _plan_bar(step: int, total: int) -> str:
    """ASCII progress bar §12.3."""
    if total <= 0:
        return ""
    filled = int(10 * step / total)
    bar = "▓" * filled + "▒" * (10 - filled)
    return bar

def _render_todo_block(args: dict, result_text: str) -> str:
    """GAP-TUI-4: Render todowrite / manage_todo call as a '# Todos' block."""
    todos = args.get("todos") or args.get("steps")
    if todos and isinstance(todos, list):
        lines = ["[bold]# Todos[/]"]
        for item in todos:
            if isinstance(item, dict):
                status = item.get("status", "pending")
                content = item.get("content") or item.get("description") or str(item)
                priority = item.get("priority", "")
                sicon = _TODO_STATUS_ICONS.get(status, "○")
                scolor = _TODO_STATUS_COLORS.get(status, "#888888")
                pri_str = f"  [dim]{priority}[/]" if priority else ""
                lines.append(f"  [{scolor}]{sicon}[/] {content}{pri_str}")
            else:
                lines.append(f"  ○ {item}")
        return "\n".join(lines)

    md_lines = result_text.strip().splitlines()
    todo_lines = [line for line in md_lines if line.strip().startswith("- [")]
    if todo_lines:
        lines = ["[bold]# Todos[/]"]
        for line in todo_lines:
            stripped = line.strip()
            if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
                text = stripped[6:].strip()
                lines.append(f"  [#22c55e]✓[/] {text}")
            elif stripped.startswith("- [~]"):
                text = stripped[5:].strip()
                lines.append(f"  [#facc15]●[/] {text}")
            else:
                text = stripped[6:].strip()
                lines.append(f"  [#888888]○[/] {text}")
        return "\n".join(lines)

    action = args.get("action", "")
    if action:
        return f"[bold #22c55e]⚙ Todo {action}[/]"
    return ""

def _render_question_block(args: dict, result_text: str) -> str:
    """GAP-TUI-5: Render question / ask_user call as a '# Questions' Q&A block."""
    import json as _json

    questions = args.get("questions")
    if questions and isinstance(questions, list):
        lines = ["[bold]# Questions[/]"]
        try:
            result_obj = (
                _json.loads(result_text) if result_text.strip().startswith("{") else {}
            )
        except Exception:
            result_obj = {}
        for q in questions:
            if isinstance(q, dict):
                header = q.get("header", "")
                qtext = q.get("question", "")
                answer = result_obj.get(header, result_obj.get(qtext, ""))
                lines.append(f"  [dim]{qtext}[/]")
                if answer:
                    lines.append(f"  {answer}")
        return "\n".join(lines)

    question = args.get("question", "")
    if question:
        lines = ["[bold]# Questions[/]", f"  [dim]{question}[/]"]
        try:
            result_obj = (
                _json.loads(result_text) if result_text.strip().startswith("{") else {}
            )
            answer = result_obj.get("answer", "")
        except Exception:
            answer = result_text.strip()
        if answer:
            lines.append(f"  {answer}")
        return "\n".join(lines)

    return ""

class AppToolHandlersMixin(AgentAppProtocol):
    """Tool-call, subagent, diff, plan, and button-handler mixin.

    Expects the host class to expose:
    - ``self._tool_widgets``, ``self._tool_args`` (dicts)
    - ``self._subagent_widgets`` (dict[str, SubagentProgress])
    - ``self._allow_always_tools`` (set[str])
    - ``self._perm_tool_names`` (dict[str, str])
    - ``self._tool_call_count`` (int)
    - ``self._pending_perm_count`` (int)
    - ``self._bridge`` (AgentBridge)
    - ``self.query_one``, ``self.call_later``, ``self.notify``, ``self._update_perm_badge``
    - ``self._sched_chat_widget``, ``self._mount_chat_widget``, ``self._prune_chat_log``
    - ``self._get_sessions_dir()`` — from AppSessionMixin
    - ``self._chat_handle_plan_requested`` — from ChatDisplayMixin
    """

    # ── Tool call 3-beat lifecycle (§6.1) ─────────────────────────────────

    @on(ToolCallStartEvent)
    def handle_tool_start(self: AgentAppProtocol, event) -> None:
        logger.info(f"Tool start: {event.tool_name}  id={event.tool_id}")
        self._tool_call_count += 1
        icon = _TOOL_ICONS.get(event.tool_name.lower(), "⠿")

        if event.tool_id:
            self._tool_args[event.tool_id] = event.tool_args

        if event.tool_name.lower() in ("bash", "run_bash"):
            cmd = event.tool_args.get("command", "")
            desc = event.tool_args.get("description", "")
            header = f"# {desc}" if desc else f"# {event.tool_name}"
            cmd_line = f"$ {cmd[:120]}{'…' if len(cmd) > 120 else ''}" if cmd else ""
            label = header + (f"\n{cmd_line}" if cmd_line else "")
        else:
            args_fmt = _fmt_args(event.tool_args)
            label = f"{icon} {event.tool_name}  {args_fmt}".rstrip()

        widget = Static(
            f"[bold #facc15]{label}[/]",
            classes="tool_msg tool_inprogress",
            markup=True,
        )
        if event.tool_id:
            self._tool_widgets[event.tool_id] = widget
        try:
            self.query_one("#sb_tool_activity", Static).update(
                f"{icon} {event.tool_name}"
            )
            self.query_one("#sb_tool_count", Static).update(str(self._tool_call_count))
        except Exception:
            pass
        self._sched_chat_widget(widget)

    @on(ToolCallFinishEvent)
    def handle_tool_finish(self: AgentAppProtocol, event) -> None:
        logger.info(f"Tool finish: {event.tool_name}  ok={event.ok}")
        icon = _TOOL_ICONS.get(event.tool_name.lower(), "✓" if event.ok else "✗")
        ok_icon = "✓" if event.ok else "✗"
        color = "#22c55e" if event.ok else "#ff5555"
        result_lines = event.result_text.strip().splitlines()

        cached_args = self._tool_args.pop(event.tool_id, {}) if event.tool_id else {}

        tool_lower = event.tool_name.lower()
        if tool_lower in ("todowrite", "manage_todo") and event.ok:
            try:
                import json

                items = json.loads(event.result_text)
                if isinstance(items, list):
                    from .components import TodoListWidget

                    widget = TodoListWidget()
                    widget.update_items(items)
                    if event.tool_id and event.tool_id in self._tool_widgets:
                        old_w = self._tool_widgets.pop(event.tool_id)
                        old_w.remove()
                    else:
                        self._sched_chat_widget(
                            Static(
                                "[bold #a78bfa]Todos updated[/]",
                                classes="tool_msg",
                                markup=True,
                            )
                        )
                    self._sched_chat_widget(widget)
                    try:
                        self.query_one("#sb_tool_activity", Static).update(
                            "⚙ todos updated"
                        )
                    except Exception:
                        pass
                    return
            except Exception:
                pass

        if tool_lower in ("question", "ask_user") and event.ok:
            finished_markup = _render_question_block(cached_args, event.result_text)
            if finished_markup:
                if event.tool_id and event.tool_id in self._tool_widgets:
                    w = self._tool_widgets.pop(event.tool_id)
                    self.call_later(
                        lambda widget=w, m=finished_markup: widget.update(m)
                    )
                else:
                    self._sched_chat_widget(
                        Static(finished_markup, classes="tool_msg", markup=True)
                    )
                try:
                    self.query_one("#sb_tool_activity", Static).update(
                        "→ question answered"
                    )
                except Exception:
                    pass
                return

        if tool_lower in ("task", "delegation") and event.ok:
            role = cached_args.get("role") or cached_args.get("agent_type") or "Agent"
            task_desc = cached_args.get("task") or cached_args.get("description") or ""
            task_short = task_desc[:60] + ("…" if len(task_desc) > 60 else "")
            tc_count = event.result_text.lower().count("tool")
            tc_str = (
                f"└ {tc_count} toolcall{'s' if tc_count != 1 else ''}"
                if tc_count
                else "└ done"
            )
            markup = (
                f"[bold #22c55e]│ {role} Task[/] — {task_short}\n  [dim]{tc_str}[/]"
            )
            if event.tool_id and event.tool_id in self._tool_widgets:
                w = self._tool_widgets.pop(event.tool_id)
                self.call_later(lambda widget=w, m=markup: widget.update(m))
            else:
                self._sched_chat_widget(Static(markup, classes="tool_msg", markup=True))
            try:
                self.query_one("#sb_tool_activity", Static).update(f"│ {role} done")
            except Exception:
                pass
            return

        if tool_lower in ("bash", "run_bash"):
            command = cached_args.get("command", "")
            desc = cached_args.get("description", "")
            from .components import BashBlock

            block = BashBlock(command=command, description=desc)
            block.set_output(result_lines)
            if event.tool_id and event.tool_id in self._tool_widgets:
                old_w = self._tool_widgets.pop(event.tool_id)
                old_w.remove()
                self._sched_chat_widget(block)
            else:
                header = Static(
                    f"[bold #fbbf24]# {desc or event.tool_name}[/]",
                    classes="tool_msg",
                    markup=True,
                )
                self._sched_chat_widget(header)
                self._sched_chat_widget(block)
            try:
                self.query_one("#sb_tool_activity", Static).update("# bash done")
            except Exception:
                pass
            return

        if len(result_lines) > 60:
            extra = len(result_lines) - 60
            result_lines = result_lines[:60] + [f"… {extra} more lines"]
        result_display = "\n".join(result_lines)
        label = f"{icon} {event.tool_name}"

        sep = "\n" if result_display else ""
        if event.tool_id and event.tool_id in self._tool_widgets:
            w = self._tool_widgets.pop(event.tool_id)
            self.call_later(
                lambda widget=w, col=color, r=result_display, lbl=label, s=sep: (
                    widget.update(f"[bold {col}]{lbl}[/]{s}{r}")
                )
            )
        else:
            widget = Static(
                f"[bold {color}]{label}[/]{sep}{result_display}",
                classes="tool_msg",
                markup=True,
            )
            self._sched_chat_widget(widget)

        try:
            self.query_one("#sb_tool_activity", Static).update(
                f"{ok_icon} {event.tool_name}"
            )
        except Exception:
            pass

    @on(ToolCallErrorEvent)
    def handle_tool_error(self: AgentAppProtocol, event) -> None:
        logger.error(f"Tool error: {event.tool_name}  {event.error}")
        if event.tool_id and event.tool_id in self._tool_widgets:
            w = self._tool_widgets.pop(event.tool_id)
            self.call_later(
                lambda widget=w, n=event.tool_name, err=event.error: widget.update(
                    f"[bold #ff5555]✗ {n} failed:[/] {err}"
                )
            )
        else:
            widget = Static(
                f"[bold #ff5555]✗ {event.tool_name} failed:[/] {event.error}",
                classes="tool_msg error_msg",
                markup=True,
            )
            self._sched_chat_widget(widget)
        try:
            self.query_one("#sb_tool_activity", Static).update(f"✗ {event.tool_name}")
        except Exception:
            pass

    # ── Subagent lifecycle (SUBAGENT-VIS-4) ──────────────────────────────

    def _update_subagent_footer(self: AgentAppProtocol) -> None:
        active = len(self._subagent_widgets)
        try:
            chip = self.query_one("#subagent_footer_chip", Button)
            if active:
                chip.label = f"⇢ {active} subagent{'s' if active > 1 else ''}"
                chip.display = True
            else:
                chip.display = False
        except Exception:
            pass

    @on(SubagentProgress.Clicked)
    async def handle_subagent_clicked(self: AgentAppProtocol, event) -> None:
        from .screens.subagent_detail import SubagentDetailScreen

        self.push_screen(
            SubagentDetailScreen(
                child_session_id=event.child_session_id,
                role=event.role,
                task=event.task,
                sessions_dir=self._get_sessions_dir(),
            )
        )

    @on(SubagentStartEvent)
    def handle_subagent_start(self: AgentAppProtocol, event) -> None:
        logger.info(f"Subagent start: {event.role}  id={event.child_session_id}")
        from .components import SubagentProgress

        widget = self._subagent_widgets.get(event.child_session_id)
        if widget is None:
            widget = SubagentProgress(event.role, event.task, event.child_session_id)
            if event.child_session_id:
                self._subagent_widgets[event.child_session_id] = widget
            self._sched_chat_widget(widget)
        try:
            active = len(self._subagent_widgets)
            self.query_one("#sb_subagent_status", Static).update(f"{active} running")
        except Exception:
            pass
        self._update_subagent_footer()

    @on(SubagentFinishEvent)
    def handle_subagent_finish(self: AgentAppProtocol, event) -> None:
        logger.info(
            f"Subagent finish: {event.role}  ok={event.ok}  id={event.child_session_id}"
        )
        widget = self._subagent_widgets.pop(event.child_session_id, None)
        if widget is not None:
            self.call_later(lambda w=widget, ok=event.ok: w.finish(ok))
        try:
            active = len(self._subagent_widgets)
            label = f"{active} running" if active else "none"
            self.query_one("#sb_subagent_status", Static).update(label)
        except Exception:
            pass
        self._update_subagent_footer()

    @on(ToolExecutionNotice)
    def handle_tool_notice(self: AgentAppProtocol, event) -> None:
        logger.info(f"Tool: {event.tool_name}")
        args_fmt = _fmt_args(event.arguments)
        widget = Static(
            f"[bold #facc15]✦ {event.tool_name}[/]  {args_fmt}",
            classes="tool_msg",
            markup=True,
        )
        self._sched_chat_widget(widget)

    # ── Diff preview — side-by-side (§6.4, T_DIFF) ───────────────────────

    @on(DiffPreviewEvent)
    async def handle_diff_preview(self: AgentAppProtocol, event) -> None:
        logger.info(f"Diff preview: {event.path}")
        use_inline = self._settings.get("diff_style", "side-by-side") == "inline"
        if use_inline:
            from .components import InlineDiff

            widget: Widget = InlineDiff(
                path=event.path,
                diff=event.diff,
                is_new_file=event.is_new_file,
            )
        else:
            widget = SideBySideDiff(
                path=event.path,
                diff=event.diff,
                is_new_file=event.is_new_file,
            )
        await self._mount_chat_widget(widget)

    @on(SideBySideDiff.Accepted)
    async def handle_diff_accepted(self: AgentAppProtocol, event) -> None:
        logger.info(f"Diff accepted: {event.path}")
        w = Static(
            f"[bold #22c55e]✓ Accepted:[/] {event.path}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)
        try:
            self._bridge.confirm_file_preview(event.path)
        except Exception:
            pass

    @on(SideBySideDiff.Rejected)
    async def handle_diff_rejected(self: AgentAppProtocol, event) -> None:
        logger.info(f"Diff rejected: {event.path}")
        w = Static(
            f"[bold #ff5555]✗ Rejected:[/] {event.path}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)
        try:
            self._bridge.reject_file_preview(event.path)
        except Exception:
            pass

    # ── Plan progress (§12.3) ─────────────────────────────────────────────

    @on(PlanProgressEvent)
    def handle_plan_progress(self: AgentAppProtocol, event) -> None:
        bar = _plan_bar(event.step, event.total)
        try:
            self.query_one("#sb_plan_bar", Static).update(
                f"{bar}  {event.step} / {event.total}"
            )
            self.query_one("#sb_plan_desc", Static).update(event.description)
        except Exception:
            pass

    # ── Plan approval UI (§14.1) ──────────────────────────────────────────

    @on(PlanRequestedEvent)
    async def handle_plan_requested(self: AgentAppProtocol, event) -> None:
        await self._chat_handle_plan_requested(event)

    # ── Bash tier-3 approval gate (§16.1) ─────────────────────────────────

    @on(BashApprovalEvent)
    async def handle_bash_approval(self: AgentAppProtocol, event) -> None:
        logger.warning(f"Bash tier-3 approval required: {event.command}")
        self._update_perm_badge(+1)
        chat_log = self.query_one("#chat_log", VerticalScroll)
        warn = Static(
            f"[bold #facc15]⚠ This command requires approval:[/]  {event.command}",
            classes="retry_msg",
            markup=True,
        )
        await chat_log.mount(warn)
        tid = event.tool_id
        row = Horizontal(id=f"bash_approval_{tid}", classes="approval_row")
        await chat_log.mount(row)
        await row.mount(Button("Allow", id=f"btn_bash_allow_{tid}", variant="success"))
        await row.mount(Button("Deny", id=f"btn_bash_deny_{tid}", variant="error"))
        chat_log.scroll_end(animate=False)
        self._prune_chat_log()

    @on(Button.Pressed)
    async def on_any_button(self: AgentAppProtocol, event) -> None:
        btn_id = event.button.id or ""

        _handled = (
            btn_id in ("btn_approve_plan", "btn_reject_plan", "subagent_footer_chip")
            or btn_id.startswith("btn_bash_allow_")
            or btn_id.startswith("btn_bash_deny_")
            or btn_id.startswith("btn_tool_perm_allow_")
            or btn_id.startswith("btn_tool_perm_deny_")
            or btn_id.startswith("btn_doom_allow_")
            or btn_id.startswith("btn_doom_deny_")
        )
        if _handled:
            event.stop()

        if btn_id == "subagent_footer_chip":
            from .screens.session_screen import SessionScreen

            self.push_screen(SessionScreen(filter_subagents=True))
            return

        if btn_id == "btn_approve_plan":
            self._bridge.approve_plan()
            try:
                self.query_one("#plan_approval").remove()
            except Exception:
                pass
            w = Static(
                "[bold #22c55e]✓ Plan approved[/]", classes="retry_msg", markup=True
            )
            await self._mount_chat_widget(w)
            self.post_message(PlanApproved())

        elif btn_id == "btn_reject_plan":
            self._bridge.reject_plan()
            try:
                self.query_one("#plan_approval").remove()
            except Exception:
                pass
            w = Static(
                "[bold #ff5555]✗ Plan rejected[/]", classes="retry_msg", markup=True
            )
            await self._mount_chat_widget(w)
            self.post_message(PlanRejected())

        elif btn_id.startswith("btn_bash_allow_"):
            tool_id = btn_id[len("btn_bash_allow_") :]
            self._update_perm_badge(-1)
            self._bridge.bash_approved(tool_id)
            try:
                self.query_one(f"#bash_approval_{tool_id}").remove()
            except Exception:
                pass
            w = Static(
                "[bold #22c55e]✓ Command allowed[/]", classes="retry_msg", markup=True
            )
            await self._mount_chat_widget(w)
            self.post_message(BashApproved(tool_id=tool_id))

        elif btn_id.startswith("btn_bash_deny_"):
            tool_id = btn_id[len("btn_bash_deny_") :]
            self._update_perm_badge(-1)
            self._bridge.bash_denied(tool_id)
            try:
                self.query_one(f"#bash_approval_{tool_id}").remove()
            except Exception:
                pass
            if tool_id in self._tool_widgets:
                w_pending = self._tool_widgets.pop(tool_id)
                self.call_later(
                    lambda wp=w_pending: wp.update(
                        "[bold #ff5555 strike]✗ bash — denied[/]"
                    )
                )
            w = Static(
                "[bold #ff5555]✗ Command denied[/]", classes="retry_msg", markup=True
            )
            await self._mount_chat_widget(w)
            self.post_message(BashDenied(tool_id=tool_id))

        elif btn_id.startswith("btn_tool_perm_allow_"):
            tool_id = btn_id[len("btn_tool_perm_allow_") :]
            self._update_perm_badge(-1)
            self._bridge.publish("tool.permission_granted", {"tool_id": tool_id})
            try:
                self.query_one(f"#tool_perm_{tool_id}").remove()
            except Exception:
                pass
            w = Static(
                "[bold #22c55e]✓ Tool allowed[/]", classes="retry_msg", markup=True
            )
            await self._mount_chat_widget(w)
            self.post_message(ToolPermissionApproved(tool_id=tool_id))

        elif btn_id.startswith("btn_tool_perm_deny_"):
            tool_id = btn_id[len("btn_tool_perm_deny_") :]
            self._update_perm_badge(-1)
            try:
                self.query_one(f"#tool_perm_{tool_id}").remove()
            except Exception:
                pass
            if tool_id in self._tool_widgets:
                w_pending = self._tool_widgets.pop(tool_id)
                self.call_later(
                    lambda wp=w_pending: wp.update(
                        "[bold #ff5555 strike]✗ tool — denied[/]"
                    )
                )
            fb_row = Horizontal(id=f"tool_deny_fb_{tool_id}", classes="approval_row")
            await self._mount_chat_widget(
                Static(
                    "[bold #ff5555]✗ Tool denied[/]  [dim]Reason (optional):[/]",
                    classes="retry_msg",
                    markup=True,
                )
            )
            await self._mount_chat_widget(fb_row)
            fb_input = Input(
                placeholder="Why was this denied? (press Enter to send)",
                id=f"inp_deny_fb_{tool_id}",
            )
            await fb_row.mount(fb_input)
            await fb_row.mount(
                Button("Send", id=f"btn_deny_fb_send_{tool_id}", variant="primary")
            )
            await fb_row.mount(
                Button("Skip", id=f"btn_deny_fb_skip_{tool_id}", variant="default")
            )
            self._bridge.publish("tool.permission_denied", {"tool_id": tool_id})
            self.post_message(ToolPermissionDenied(tool_id=tool_id))

        elif btn_id.startswith("btn_deny_fb_send_"):
            tool_id = btn_id[len("btn_deny_fb_send_") :]
            feedback = ""
            try:
                feedback = self.query_one(
                    f"#inp_deny_fb_{tool_id}", Input
                ).value.strip()
                self.query_one(f"#tool_deny_fb_{tool_id}").remove()
            except Exception:
                pass
            if feedback:
                self._bridge.publish(
                    "tool.denial_feedback",
                    {"tool_id": tool_id, "feedback": feedback},
                )
                w = Static(
                    f"[dim]Feedback sent: {feedback}[/]",
                    classes="retry_msg",
                    markup=True,
                )
                await self._mount_chat_widget(w)

        elif btn_id.startswith("btn_deny_fb_skip_"):
            tool_id = btn_id[len("btn_deny_fb_skip_") :]
            try:
                self.query_one(f"#tool_deny_fb_{tool_id}").remove()
            except Exception:
                pass

        elif btn_id.startswith("btn_tool_perm_always_"):
            tool_id = btn_id[len("btn_tool_perm_always_") :]
            self._update_perm_badge(-1)
            self._bridge.publish("tool.permission_granted", {"tool_id": tool_id})
            try:
                perm_row = self.query_one(f"#tool_perm_{tool_id}")
                _perm_names = getattr(self, "_perm_tool_names", {})
                tool_name = _perm_names.get(tool_id, tool_id)
                self._allow_always_tools.add(tool_name)
                try:
                    from src.core.orchestration.permission_policy import (
                        PermissionRule,
                        Behavior,
                        get_permission_policy,
                    )

                    policy = get_permission_policy()
                    policy.add_rule(  # type: ignore[attr-defined]
                        PermissionRule(pattern=tool_name, behavior=Behavior.ALLOW)
                    )
                    policy.save()
                    logger.info(f"Allow always persisted for: {tool_name}")
                except Exception as e:
                    logger.warning(f"Failed to persist allow-always rule: {e}")
                perm_row.remove()
            except Exception:
                pass
            w = Static(
                "[bold #f59e0b]✓ Tool allowed (always)[/]",
                classes="retry_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            self.post_message(ToolPermissionApproved(tool_id=tool_id))

        elif btn_id.startswith("btn_doom_allow_"):
            tid = btn_id[len("btn_doom_allow_") :]
            self._bridge.publish("tool.doom_loop_continue", {"tool_id": tid})
            try:
                self.query_one(f"#doom_row_{tid}").remove()
            except Exception:
                pass
            self.notify(
                "Continuing despite loop — monitor carefully", severity="warning"
            )

        elif btn_id.startswith("btn_doom_deny_"):
            tid = btn_id[len("btn_doom_deny_") :]
            self._bridge.publish("agent.interrupt", {})
            try:
                self.query_one(f"#doom_row_{tid}").remove()
            except Exception:
                pass
            self.notify("Agent stopped", severity="warning")
