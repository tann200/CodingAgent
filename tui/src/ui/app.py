"""
AgentApp — TUI System Specification v2.0 compliant Textual application.
Wires exclusively through AgentBridge → EventBus; never imports src.core directly.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from textual.notifications import SeverityLevel

from textual.app import App, ComposeResult
from textual.widgets import Header, Static, Label, Input, Button
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual import on
from textual.reactive import reactive

from .components import (
    HistoryInput,
    AgentArtifact,
    ThinkingProcess,
    StreamView,
    ConsolePanel,
    SideBySideDiff,
    ChatTextArea,
    FilePickerOverlay,
)
from .settings import SettingsStore
from .logging import get_logger
from .bus import (
    StreamChunkEvent,
    StreamingThinkingUpdate,
    DisplayReasoning,
    StatusUpdate,
    ToolExecutionNotice,
    AgentFinalResponse,
    WorkerError,
    RoleTransitionEvent,
    TokenUsageEvent,
    TaskQueueUpdatedEvent,
    FileModifiedEvent,
    TaskEscalatedEvent,
    ContextDegradedEvent,
    RetryAttemptEvent,
    RetrySucceededEvent,
    RetryFailedEvent,
    ProviderStatusChangeEvent,
    SystemSettingsLoaded,
    # New spec-compliant events
    ToolCallStartEvent,
    ToolCallFinishEvent,
    ToolCallErrorEvent,
    DiffPreviewEvent,
    PlanProgressEvent,
    PlanRequestedEvent,
    TokenBudgetEvent,
    BashApprovalEvent,
    NotificationEvent,
    SessionHealthEvent,
    ModelRoutingEvent,
    OrchestratorReadyEvent,
    AgentRunningEvent,
    GitBranchEvent,
    # Step boundary / MCP / tool permission
    StepStartEvent,
    StepFinishEvent,
    McpServerStatusEvent,
    ToolPermissionEvent,
    UsageTurnSummaryEvent,
    DoomLoopEvent,
)
from .events import (
    PaletteCommand,
    ConnectProvider,
    UpdateSettings,
    SlashCommand,
    AgentInterrupt,
    SaveProviderCredentials,
    UpdateRoleModel,
    RequestSystemSettings,
    PlanApproved,
    PlanRejected,
    BashApproved,
    BashDenied,
    ToolPermissionApproved,
    ToolPermissionDenied,
)

logger = get_logger("app")

# LOW-11 fix: extract cost-rate constants so they are easy to update and
# shared between both token-event handlers (previously each handler had its
# own hardcoded formula, making them inconsistent).
# Rates are per-1 000 tokens in USD (adjust as model pricing changes).
_COST_INPUT_PER_1K: float = 0.001  # input / system / task tokens
_COST_OUTPUT_PER_1K: float = 0.003  # output tokens

ROLE_LABELS = {
    "lead_architect": "LEAD ARCHITECT",
    "full_stack_engineer": "FULL STACK ENGINEER",
    "qa_lead": "QA LEAD",
    "system": "SYSTEM",
}
ROLE_COLORS = {
    "lead_architect": "#a855f7",
    "full_stack_engineer": "#3b82f6",
    "qa_lead": "#22c55e",
    "system": "#666666",
}

SLASH_HELP = """\
Available commands:
  /help          — show this list
  /clear         — clear chat output (keeps history)
  /new  /reset   — new session (clears history)
  /compact       — compact conversation context
  /continue      — restore & re-run previous task
  /interrupt     — cancel running agent
  /status        — show agent/provider/model status
  /fast          — switch to fastest/smallest model (NANO tier)
  /provider [n]  — list or switch provider by index/name
  /model [n]     — list or switch model for active provider
  /settings      — open settings screen
  /sessions      — browse saved sessions
  /timeline      — view session message timeline
  /diff          — show working-directory diff since last snapshot
  /fork          — fork current session to a new independent copy
  /mcp [list|add <name> <cmd…>|status] — manage MCP servers
  /quit          — exit the application"""

SAFE_SLASH_CMDS = {"interrupt", "status", "help", "quit"}

# Max chars of a single @-referenced file inlined into prompt
_AT_FILE_MAX_CHARS = 8000
# Workspace dirs to skip when scanning files for @ picker
_AT_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}


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


def _budget_color(percent: float) -> str:
    """Token budget colour coding §12.4."""
    if percent >= 86:
        return "#ff5555"
    if percent >= 61:
        return "#facc15"
    return "#22c55e"


def _plan_bar(step: int, total: int) -> str:
    """ASCII progress bar §12.3."""
    if total <= 0:
        return ""
    filled = int(10 * step / total)
    bar = "▓" * filled + "▒" * (10 - filled)
    return bar


class AgentApp(App[None]):
    COMMAND_PALETTE = False
    BINDINGS = [
        ("tab", "toggle_mode", "agents"),
        ("ctrl+l", "toggle_console", "console"),
        ("ctrl+o", "show_commands", "commands"),
        ("ctrl+s", "open_settings", "settings"),
        ("ctrl+m", "open_model_picker", "model"),
        ("ctrl+q", "action_quit_app", "quit"),
    ]
    CSS_PATH = "styles/app.tcss"

    active_role = reactive("system")
    total_tokens = reactive(0)
    context_window = reactive(32000)
    pending_tasks = reactive(0)
    queue_size = reactive(0)
    is_streaming = reactive(False)
    agent_running = reactive(False)  # gates input field

    def __init__(self) -> None:
        super().__init__()
        import uuid as _uuid
        from .core_bridge import AgentBridge

        # TUI-01: Accept initial working dir injected by main.py before run().
        _init_wd: Optional[Path] = getattr(self, "_initial_working_dir", None)
        self._bridge = AgentBridge(self, working_dir=_init_wd)

        # TASK-05: stable UUID for session snapshots (generated once per app launch)
        self._session_id: str = str(_uuid.uuid4())

        self._current_stream: Optional[StreamView] = None
        self._role_cycle = ["lead_architect", "full_stack_engineer", "qa_lead"]
        self._role_idx = 0
        self._modified_files: list[str] = []
        self._settings = SettingsStore()
        self._last_esc_time: float = 0.0

        # Per-tool in-progress widget tracking
        self._tool_widgets: dict[str, Static] = {}
        # Continue state for /continue command
        self._continue_state: Optional[dict] = None
        # Last task text for /continue
        self._last_task_text: str = ""
        # Sidebar counters
        self._tool_call_count: int = 0
        self._session_input_tokens: int = 0
        self._session_output_tokens: int = 0

        # ── @file picker state ────────────────────────────────────────────
        self._at_picker_active: bool = False
        self._at_picker_matches: list[str] = []
        self._at_picker_index: int = 0
        self._at_picker_widget: Optional[FilePickerOverlay] = None
        self._at_prefix: str = ""
        self._at_file_cache: list[str] = []
        self._at_file_cache_ts: float = 0.0

        # ── Inline command palette state (drives ChatTextArea routing) ────
        self._palette_active: bool = False
        self._palette_matches: list[str] = []
        self._palette_index: int = 0

        logger.info("AgentApp initialized")

    # ── Layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("MOCK ENGINE", id="provider_banner", classes="connected")

        with Horizontal(id="main_workspace"):
            yield ConsolePanel(id="console_panel")

            with Vertical(id="left_column"):
                yield VerticalScroll(id="chat_log")

                # File picker overlay sits just above the input
                yield FilePickerOverlay(id="file_picker")

                yield ChatTextArea(
                    id="user_input",
                    placeholder="Type a message or /help … (Esc×2 to interrupt)",
                )

            with VerticalScroll(id="right_sidebar"):
                # ── Task status ────────────────────────────────────────────
                yield Label("TASK", classes="sb_title")
                yield Static("idle", id="sb_task_status")

                # ── Plan progress ──────────────────────────────────────────
                yield Label("PLAN PROGRESS", classes="sb_title")
                yield Static("—", id="sb_plan_bar")
                yield Static("", id="sb_plan_desc")

                # ── Tool activity ──────────────────────────────────────────
                yield Label("LAST TOOL", classes="sb_title")
                yield Static("—", id="sb_tool_activity")

                # ── Token budget ───────────────────────────────────────────
                yield Label("TOKEN BUDGET", classes="sb_title")
                yield Static("0 / 32,000  (0.0%)", id="sb_tokens")

                # ── Token breakdown (in / out) ────────────────────────────
                yield Label("TOKEN BREAKDOWN", classes="sb_title")
                yield Static("In: 0 | Out: 0", id="sb_context")

                # ── Session cost ───────────────────────────────────────────
                yield Label("SESSION COST", classes="sb_title")
                yield Static("$0.000", id="sb_cost")

                # ── Provider / model ───────────────────────────────────────
                yield Label("PROVIDER / MODEL", classes="sb_title")
                yield Static("disconnected", id="sb_provider")
                yield Static("—", id="sb_model_info")

                # ── Git branch ─────────────────────────────────────────────
                yield Label("GIT", classes="sb_title")
                yield Static("○ —", id="sb_git")

                # ── Working directory ──────────────────────────────────────
                yield Label("WORKING DIR", classes="sb_title")
                yield Static(".", id="sb_workdir")

                # ── Active role ────────────────────────────────────────────
                yield Label("ACTIVE ROLE", classes="sb_title")
                yield Static("system", id="sb_role")

                # ── Tools called ───────────────────────────────────────────
                yield Label("TOOLS CALLED", classes="sb_title")
                yield Static("0", id="sb_tool_count")

                # ── Session ────────────────────────────────────────────────
                yield Label("SESSION", classes="sb_title")
                yield Static("Pending: 0 | Queue: 0", id="sb_session")
                yield Static("Status: idle", id="sb_status")

                # ── Files modified ─────────────────────────────────────────
                yield Label("FILES MODIFIED", classes="sb_title")
                yield Static("None", id="sb_files")

        with Horizontal(id="coding_footer"):
            yield Static(id="status_left", markup=True)
            yield Static(
                "tab [b]agents[/b]  ctrl+l [b]console[/b]  "
                "ctrl+o [b]commands[/b]  ctrl+s [b]settings[/b]  "
                "esc×2 [b]stop[/b]  ctrl+q [b]quit[/b]",
                id="status_right",
                markup=True,
            )
            yield Static("", id="mcp_status_chip", markup=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        # §10.1 startup sequence
        self._bridge.setup_subscriptions()
        self._bridge.load_history()

        # Wire the file picker overlay reference now that the DOM is live
        try:
            self._at_picker_widget = self.query_one("#file_picker", FilePickerOverlay)
        except Exception:
            pass

        self._update_role_display("system")
        self._update_status_bar()

        console = self.query_one("#console_panel", ConsolePanel)
        if not self._settings.get("console_visible", False):
            console.add_class("hidden")
        saved_theme = self._settings.get("theme", "textual-dark")
        try:
            self.theme = saved_theme
        except Exception:
            pass

        # Load frecency prompt history into the ChatTextArea
        try:
            inp = self.query_one("#user_input", ChatTextArea)
            inp._prompt_history = self._bridge.load_prompt_history()
        except Exception:
            pass

        # Request system settings (backwards compat with mock engine)
        self.post_message(RequestSystemSettings())
        # §10.1 step 6 — trigger backend hydration
        self._bridge.publish_session_request()

        logger.info("TUI mounted and ready")

    def on_unmount(self) -> None:
        """§10.2 shutdown sequence — called automatically by Textual on exit."""
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        self._save_session_snapshot()
        self._bridge.interrupt()
        self._bridge.save_history()
        self._bridge.cleanup()
        logger.info("TUI unmounted — bridge cleaned up")

    def action_quit_app(self) -> None:
        """§10.2 — clean shutdown via ctrl+q."""
        self._save_session_snapshot()
        self.exit()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _update_role_display(self, role: str) -> None:
        label = ROLE_LABELS.get(role, role.upper().replace("_", " "))
        color = ROLE_COLORS.get(role, "#888888")
        try:
            self.query_one("#sb_role", Static).update(f"[bold {color}]{label}[/]")
            self.sub_title = f"AGENT: {label}"
        except Exception:
            pass

    def _update_status_bar(self) -> None:
        role = self.active_role
        label = ROLE_LABELS.get(role, role.upper().replace("_", " "))
        color = ROLE_COLORS.get(role, "#888888")
        streaming = " [bold #facc15][streaming][/]" if self.is_streaming else ""
        running = " [bold #ff5555][running][/]" if self.agent_running else ""
        try:
            self.query_one("#status_left", Static).update(
                f"[bold {color}]{label}[/] | "
                f"Tokens: {self.total_tokens:,}/{self.context_window:,}"
                f"{streaming}{running}"
            )
        except Exception:
            pass

    def _ensure_stream_widget(self) -> StreamView:
        if self._current_stream is None:
            self.is_streaming = True
            role = ROLE_LABELS.get(self.active_role, self.active_role)
            self._current_stream = StreamView(role=role, classes="stream_msg")
            chat_log = self.query_one("#chat_log", VerticalScroll)
            self.call_later(
                lambda w=self._current_stream, c=chat_log: asyncio.ensure_future(
                    self._mount_and_scroll(w, c)
                )
            )
        return self._current_stream

    async def _mount_and_scroll(self, widget, container) -> None:
        await container.mount(widget)
        container.scroll_end(animate=False)

    def _finalize_stream(self) -> None:
        if self._current_stream is not None:
            self._current_stream = None
            self.is_streaming = False
            self._update_status_bar()

    def _append_log_line(self, line: str, level: str = "INFO") -> None:
        """§16.4 — write log.new events DIRECTLY to console, never through logging."""
        try:
            console = self.query_one("#console_panel", ConsolePanel)
            console.write_line(line, level)
        except Exception:
            pass

    async def _mount_chat_widget(self, widget) -> None:
        chat_log = self.query_one("#chat_log", VerticalScroll)
        await chat_log.mount(widget)
        chat_log.scroll_end(animate=False)

    def _sched_chat_widget(self, widget) -> None:
        """Schedule a widget mount in the chat log from a sync handler."""
        chat_log = self.query_one("#chat_log", VerticalScroll)
        self.call_later(
            lambda w=widget, c=chat_log: asyncio.ensure_future(
                self._mount_and_scroll(w, c)
            )
        )

    # ── Session snapshot ──────────────────────────────────────────────────

    def _get_sessions_dir(self) -> Path:
        d = Path.home() / ".coding_agent" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_session_snapshot(self) -> None:
        """Snapshot current session to ~/.coding_agent/sessions/{session_id}.json.

        TASK-05: enriched payload includes version, session_id, turn_count,
        input_tokens, output_tokens for SessionListScreen + resumption.
        """
        import json
        import tempfile

        try:
            with self._bridge._history_lock:
                history = list(self._bridge.history)
            if not history:
                return
            first_user = next(
                (text[:60] for role, text in history if role == "user"), ""
            )
            # TASK-05: pull turn count and token totals from bridge accessors
            turn_count = self._bridge.get_turn_count()
            input_tokens, output_tokens = self._bridge.get_usage_totals()
            created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            payload = {
                "version": 1,
                "session_id": self._session_id,
                "timestamp": time.time(),
                "created_at": created_at,
                "task_name": first_user,
                "message_count": len(history),
                "turn_count": turn_count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "messages": [{"role": r, "content": t} for r, t in history],
                "working_dir": self._bridge.working_dir or str(os.getcwd()),
            }
            p = self._get_sessions_dir() / f"session_{self._session_id}.json"
            fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp_path, str(p))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:
            pass

    # ── §10.3 New session ─────────────────────────────────────────────────

    def _handle_session_new(self) -> None:
        """Called from bridge when session.new fires."""
        self._clear_chat_panel()
        self._reset_sidebar()

    def _clear_chat_panel(self) -> None:
        self._finalize_stream()
        self._tool_widgets.clear()
        try:
            self.query_one("#chat_log", VerticalScroll).remove_children()
        except Exception:
            pass

    def _reset_sidebar(self) -> None:
        self._modified_files.clear()
        self.total_tokens = 0
        self.pending_tasks = 0
        self.queue_size = 0
        self._tool_call_count = 0
        self._session_input_tokens = 0
        self._session_output_tokens = 0
        try:
            self.query_one("#sb_task_status", Static).update("idle")
            self.query_one("#sb_plan_bar", Static).update("—")
            self.query_one("#sb_plan_desc", Static).update("")
            self.query_one("#sb_tool_activity", Static).update("—")
            self.query_one("#sb_session", Static).update("Pending: 0 | Queue: 0")
            self.query_one("#sb_tokens", Static).update("0 / 32,000  (0.0%)")
            self.query_one("#sb_context", Static).update("In: 0 | Out: 0")
            self.query_one("#sb_cost", Static).update("$0.000")
            self.query_one("#sb_git", Static).update("○ —")
            self.query_one("#sb_tool_count", Static).update("0")
            self.query_one("#sb_files", Static).update("None")
            self.query_one("#sb_status", Static).update("Status: idle")
            self._update_status_bar()
        except Exception as e:
            logger.error(f"Error resetting sidebar: {e}")

    # ── @file picker helpers ───────────────────────────────────────────────

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

    # ── Inline palette helpers ─────────────────────────────────────────────

    def _palette_navigate(self, direction: str) -> None:
        """Move the palette selection up or down (for ChatTextArea routing)."""
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

    # ── @file token expansion ─────────────────────────────────────────────

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
                # Ensure path stays inside workspace
                if not str(target).startswith(str(wd)):
                    return m.group(0)
                if target.is_file():
                    content = target.read_text(errors="replace")[:_AT_FILE_MAX_CHARS]
                    return f"<file: {rel}>\n{content}\n</file>"
            except Exception:
                pass
            return m.group(0)

        return re.sub(r"@(\S+)", _replace, text)

    # ── EventBus / bus event handlers ─────────────────────────────────────

    @on(SystemSettingsLoaded)
    def handle_system_settings(self, event: SystemSettingsLoaded) -> None:
        logger.info(f"System settings loaded: {len(event.settings)} keys")
        self._settings.apply_system_settings(event.settings, event.providers)
        ctx = event.settings.get("context_window", 32000)
        try:
            self.context_window = int(ctx)
        except (ValueError, TypeError):
            pass
        saved_theme = event.settings.get("theme", "textual-dark")
        try:
            self.theme = saved_theme
        except Exception:
            pass
        self._update_status_bar()

    @on(OrchestratorReadyEvent)
    def handle_orchestrator_ready(self, event: OrchestratorReadyEvent) -> None:
        logger.info(f"Orchestrator ready: working_dir={event.working_dir}")
        try:
            self.query_one("#sb_workdir", Static).update(event.working_dir)
            self.query_one("#provider_banner", Static).update(f"  {event.working_dir}")
        except Exception:
            pass

    @on(AgentRunningEvent)
    def handle_agent_running(self, event: AgentRunningEvent) -> None:
        self.agent_running = event.running
        self._update_status_bar()
        try:
            inp = self.query_one("#user_input", ChatTextArea)
            if event.running:
                inp.add_class("input_locked")
            else:
                inp.remove_class("input_locked")
        except Exception:
            pass

    @on(ModelRoutingEvent)
    def handle_model_routing(self, event: ModelRoutingEvent) -> None:
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

    @on(StreamChunkEvent)
    def handle_stream_chunk(self, event: StreamChunkEvent) -> None:
        stream = self._ensure_stream_widget()
        stream.append_chunk(event.chunk)
        if not event.is_partial:
            self._finalize_stream()
        try:
            self.query_one("#chat_log", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    @on(StreamingThinkingUpdate)
    def handle_thinking_update(self, event: StreamingThinkingUpdate) -> None:
        stream = self._ensure_stream_widget()
        stream.append_chunk(event.content)
        if event.is_complete:
            self._finalize_stream()

    @on(DisplayReasoning)
    async def handle_reasoning(self, event: DisplayReasoning) -> None:
        self._finalize_stream()
        widget = ThinkingProcess(event.content, event.start_time)
        await self._mount_chat_widget(widget)

    @on(AgentFinalResponse)
    async def handle_final_response(self, event: AgentFinalResponse) -> None:
        self._finalize_stream()
        logger.info("Agent final response received")
        artifact = AgentArtifact(
            content=event.content, title="Response", kind="markdown"
        )
        await self._mount_chat_widget(artifact)

    @on(WorkerError)
    async def handle_error(self, event: WorkerError) -> None:
        self._finalize_stream()
        logger.error(f"Worker error: {event.message}")
        widget = Static(
            f"[bold red]✗ Error:[/] {event.message}",
            classes="error_msg",
            markup=True,
        )
        await self._mount_chat_widget(widget)

    # ── Step boundary events ───────────────────────────────────────────────

    @on(StepStartEvent)
    def handle_step_start(self, event: StepStartEvent) -> None:
        label = f"⟳ {event.tool}"
        if event.step and event.total:
            label = f"⟳ {event.tool} [{event.step}/{event.total}]"
        try:
            self.query_one("#sb_tool_activity", Static).update(label)
        except Exception:
            pass

    @on(StepFinishEvent)
    def handle_step_finish(self, event: StepFinishEvent) -> None:
        icon = "✓" if event.ok else "✗"
        elapsed = f" {event.elapsed_ms}ms" if event.elapsed_ms is not None else ""
        label = f"{icon} {event.tool}{elapsed}"
        try:
            self.query_one("#sb_tool_activity", Static).update(label)
        except Exception:
            pass

    # ── MCP server status chip ─────────────────────────────────────────────

    @on(McpServerStatusEvent)
    def handle_mcp_status(self, event: McpServerStatusEvent) -> None:
        if event.running:
            label = (
                f"[green]MCP ●[/green] {event.count}"
                if event.count
                else "[green]MCP ●[/green]"
            )
        else:
            label = "[dim]MCP ○[/dim]"
        try:
            self.query_one("#mcp_status_chip", Static).update(label)
        except Exception:
            pass

    # ── Tool permission gate ───────────────────────────────────────────────

    @on(ToolPermissionEvent)
    async def handle_tool_permission(self, event: ToolPermissionEvent) -> None:
        logger.warning(f"Tool permission required: {event.tool}  id={event.tool_id}")
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
        chat_log.scroll_end(animate=False)

    # ── Per-turn usage summary (TUI-T6) ──────────────────────────────────

    @on(UsageTurnSummaryEvent)
    def handle_usage_turn_summary(self, event: UsageTurnSummaryEvent) -> None:
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

    # ── Doom-loop gate (PERM-W3) ──────────────────────────────────────────

    @on(DoomLoopEvent)
    async def handle_doom_loop(self, event: DoomLoopEvent) -> None:
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

    # ── Tool call 3-beat lifecycle (§6.1) ─────────────────────────────────

    @on(ToolCallStartEvent)
    def handle_tool_start(self, event: ToolCallStartEvent) -> None:
        logger.info(f"Tool start: {event.tool_name}  id={event.tool_id}")
        self._tool_call_count += 1
        args_fmt = _fmt_args(event.tool_args)
        widget = Static(
            f"[bold #facc15]⠿[/] [bold]{event.tool_name}[/]  {args_fmt}",
            classes="tool_msg tool_inprogress",
            markup=True,
        )
        if event.tool_id:
            self._tool_widgets[event.tool_id] = widget
        try:
            self.query_one("#sb_tool_activity", Static).update(f"⠿ {event.tool_name}")
            self.query_one("#sb_tool_count", Static).update(str(self._tool_call_count))
        except Exception:
            pass
        self._sched_chat_widget(widget)

    @on(ToolCallFinishEvent)
    def handle_tool_finish(self, event: ToolCallFinishEvent) -> None:
        logger.info(f"Tool finish: {event.tool_name}  ok={event.ok}")
        icon = "✓" if event.ok else "✗"
        color = "#22c55e" if event.ok else "#ff5555"
        result_lines = event.result_text.strip().splitlines()
        if len(result_lines) > 60:
            extra = len(result_lines) - 60
            result_lines = result_lines[:60] + [f"… {extra} more lines"]
        result_display = "\n".join(result_lines)

        sep = "\n" if result_display else ""
        if event.tool_id and event.tool_id in self._tool_widgets:
            w = self._tool_widgets.pop(event.tool_id)
            self.call_later(
                lambda widget=w,
                ic=icon,
                col=color,
                r=result_display,
                n=event.tool_name,
                s=sep: widget.update(f"[bold {col}]{ic} {n}[/]{s}{r}")
            )
        else:
            widget = Static(
                f"[bold {color}]{icon} {event.tool_name}[/]{sep}{result_display}",
                classes="tool_msg",
                markup=True,
            )
            self._sched_chat_widget(widget)

        try:
            self.query_one("#sb_tool_activity", Static).update(
                f"{icon} {event.tool_name}"
            )
        except Exception:
            pass

    @on(ToolCallErrorEvent)
    def handle_tool_error(self, event: ToolCallErrorEvent) -> None:
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

    # ── Old-style tool notice (backwards compat) ──────────────────────────

    @on(ToolExecutionNotice)
    def handle_tool_notice(self, event: ToolExecutionNotice) -> None:
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
    async def handle_diff_preview(self, event: DiffPreviewEvent) -> None:
        logger.info(f"Diff preview: {event.path}")
        widget = SideBySideDiff(
            path=event.path,
            diff=event.diff,
            is_new_file=event.is_new_file,
        )
        await self._mount_chat_widget(widget)

    @on(SideBySideDiff.Accepted)
    async def handle_diff_accepted(self, event: SideBySideDiff.Accepted) -> None:
        logger.info(f"Diff accepted: {event.path}")
        w = Static(
            f"[bold #22c55e]✓ Accepted:[/] {event.path}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)

    @on(SideBySideDiff.Rejected)
    async def handle_diff_rejected(self, event: SideBySideDiff.Rejected) -> None:
        logger.info(f"Diff rejected: {event.path}")
        w = Static(
            f"[bold #ff5555]✗ Rejected:[/] {event.path}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)

    # ── Plan progress (§12.3) ─────────────────────────────────────────────

    @on(PlanProgressEvent)
    def handle_plan_progress(self, event: PlanProgressEvent) -> None:
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
    async def handle_plan_requested(self, event: PlanRequestedEvent) -> None:
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

    # ── Bash tier-3 approval gate (§16.1) ─────────────────────────────────

    @on(BashApprovalEvent)
    async def handle_bash_approval(self, event: BashApprovalEvent) -> None:
        logger.warning(f"Bash tier-3 approval required: {event.command}")
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

    @on(Button.Pressed)
    async def on_any_button(self, event: Button.Pressed) -> None:
        """Single handler for all buttons — avoids double-dispatch."""
        btn_id = event.button.id or ""
        event.stop()

        # ── Plan approval ───────────────────────────────────────────────
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

        # ── Bash approval ───────────────────────────────────────────────
        elif btn_id.startswith("btn_bash_allow_"):
            tool_id = btn_id[len("btn_bash_allow_") :]
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
            self._bridge.bash_denied(tool_id)
            try:
                self.query_one(f"#bash_approval_{tool_id}").remove()
            except Exception:
                pass
            w = Static(
                "[bold #ff5555]✗ Command denied[/]", classes="retry_msg", markup=True
            )
            await self._mount_chat_widget(w)
            self.post_message(BashDenied(tool_id=tool_id))

        # ── Tool permission approval ────────────────────────────────────
        elif btn_id.startswith("btn_tool_perm_allow_"):
            tool_id = btn_id[len("btn_tool_perm_allow_") :]
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
            self._bridge.publish("tool.permission_denied", {"tool_id": tool_id})
            try:
                self.query_one(f"#tool_perm_{tool_id}").remove()
            except Exception:
                pass
            w = Static(
                "[bold #ff5555]✗ Tool denied[/]", classes="retry_msg", markup=True
            )
            await self._mount_chat_widget(w)
            self.post_message(ToolPermissionDenied(tool_id=tool_id))

        # Doom-loop buttons (PERM-W3)
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

    # ── Token budget (§12.4) ──────────────────────────────────────────────

    @on(TokenBudgetEvent)
    def handle_token_budget(self, event: TokenBudgetEvent) -> None:
        color = _budget_color(event.percent)
        self.total_tokens = event.used
        self.context_window = event.limit
        # LOW-11 fix: use the shared cost-rate constants; the old formula used a
        # flat input rate (0.001) which understated cost for output-heavy sessions.
        cost = event.used / 1000 * _COST_INPUT_PER_1K
        try:
            self.query_one("#sb_tokens", Static).update(
                f"[bold {color}]{event.used:,} / {event.limit:,}  ({event.percent:.1f}%)[/]"
            )
            self.query_one("#sb_cost", Static).update(f"${cost:.4f}")
        except Exception:
            pass
        if event.warning:
            self.notify(
                f"Token budget at {event.percent:.0f}% — consider /compact",
                severity="warning",
                timeout=6,
            )
        self._update_status_bar()

    # ── Git branch (T_SIDEBAR) ─────────────────────────────────────────────

    @on(GitBranchEvent)
    def handle_git_branch(self, event: GitBranchEvent) -> None:
        dirty_mark = " [bold #facc15]*[/]" if event.dirty else ""
        ahead_behind = ""
        if event.ahead:
            ahead_behind += f" ↑{event.ahead}"
        if event.behind:
            ahead_behind += f" ↓{event.behind}"
        dot = "[bold #22c55e]●[/]" if event.branch else "[dim]○[/]"
        try:
            self.query_one("#sb_git", Static).update(
                f"{dot} {event.branch}{ahead_behind}{dirty_mark}",
            )
        except Exception:
            pass

    # ── Old token usage (backwards compat) ────────────────────────────────

    @on(TokenUsageEvent)
    def handle_token_usage(self, event: TokenUsageEvent) -> None:
        self.total_tokens = event.total or event.total_tokens
        self.context_window = event.model_window or 32000
        used_pct = (
            (self.total_tokens / self.context_window * 100)
            if self.context_window
            else 0
        )
        color = _budget_color(used_pct)
        # LOW-12 fix: event.tools contains tool-call token counts which are
        # re-injected as *input* context on the next turn, not model output
        # tokens.  Accumulate them in the input counter rather than output.
        self._session_input_tokens += event.system + event.task + event.tools
        # LOW-11 fix: use the shared cost-rate constants.
        cost = (
            self._session_input_tokens / 1000 * _COST_INPUT_PER_1K
            + self._session_output_tokens / 1000 * _COST_OUTPUT_PER_1K
        )
        try:
            self.query_one("#sb_tokens", Static).update(
                f"[bold {color}]{self.total_tokens:,} / {self.context_window:,}  ({used_pct:.1f}%)[/]"
            )
            self.query_one("#sb_context", Static).update(
                f"In: {event.system + event.task:,} | Out: {event.tools:,}"
            )
            self.query_one("#sb_cost", Static).update(f"${cost:.4f}")
            self._update_status_bar()
        except Exception as e:
            logger.error(f"Error updating token display: {e}")

    # ── Notifications (§4.5 ui.notification) ─────────────────────────────

    @on(NotificationEvent)
    def handle_notification(self, event: NotificationEvent) -> None:
        severity_map = {
            "success": "information",
            "error": "error",
            "warning": "warning",
            "info": "information",
        }
        sev = severity_map.get(event.level, "information")
        self.notify(event.message, severity=cast("SeverityLevel", sev), timeout=5)

    # ── Session health ────────────────────────────────────────────────────

    @on(SessionHealthEvent)
    async def handle_session_health(self, event: SessionHealthEvent) -> None:
        color_map = {"error": "#ff5555", "warning": "#facc15", "info": "#3b82f6"}
        color = color_map.get(event.level, "#888888")
        widget = Static(
            f"[bold {color}]⚠ {event.title}:[/] {event.message}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(widget)

    # ── Status + role ─────────────────────────────────────────────────────

    @on(StatusUpdate)
    def handle_status(self, event: StatusUpdate) -> None:
        logger.info(f"Status: {event.message}")
        try:
            self.query_one("#sb_status", Static).update(f"Status: {event.message}")
        except Exception:
            pass
        # TUI-T1: Status messages are non-conversation events; use a transient toast
        # instead of polluting the chat log.
        self.notify(event.message, timeout=3)

    @on(RoleTransitionEvent)
    async def handle_role_transition(self, event: RoleTransitionEvent) -> None:
        self._finalize_stream()
        self.active_role = event.to_role
        self._update_role_display(event.to_role)
        self._update_status_bar()
        logger.info(f"Role transition: {event.from_role} → {event.to_role}")
        color = ROLE_COLORS.get(event.to_role, "#888888")
        label = ROLE_LABELS.get(event.to_role, event.to_role.upper().replace("_", " "))
        from_label = ROLE_LABELS.get(
            event.from_role, event.from_role.upper().replace("_", " ")
        )
        widget = Static(
            f"[bold {color}]>> {from_label} → {label}[/]",
            classes="system_msg",
            markup=True,
        )
        await self._mount_chat_widget(widget)

    @on(ProviderStatusChangeEvent)
    def handle_provider_status(self, event: ProviderStatusChangeEvent) -> None:
        logger.info(
            f"Provider {event.provider}: {event.old_status} → {event.new_status}"
        )
        try:
            self.query_one("#sb_provider", Static).update(
                f"{event.provider}: {event.new_status}"
            )
            banner = self.query_one("#provider_banner", Static)
            banner.update(f"  {event.provider.upper()}  —  {event.new_status}")
            banner.remove_class("connected", "error")
            if event.new_status == "connected":
                banner.add_class("connected")
            elif event.new_status in ("error", "failed"):
                banner.add_class("error")
        except Exception:
            pass

    @on(TaskQueueUpdatedEvent)
    def handle_task_queue(self, event: TaskQueueUpdatedEvent) -> None:
        self.pending_tasks = event.pending_count
        self.queue_size = event.queue_size
        try:
            self.query_one("#sb_session", Static).update(
                f"Pending: {event.pending_count} | Queue: {event.queue_size}"
            )
        except Exception:
            pass

    @on(FileModifiedEvent)
    async def handle_file_modified(self, event: FileModifiedEvent) -> None:
        logger.info(f"File modified: {event.file_path}")
        if event.file_path and event.file_path not in self._modified_files:
            self._modified_files.append(event.file_path)
        try:
            lines = []
            for fp in self._modified_files[-5:]:
                if fp.startswith("[deleted]"):
                    lines.append(f"[bold #ff5555]✗[/] {fp[9:].strip()}")
                else:
                    lines.append(f"[#22c55e]✓[/] {fp}")
            self.query_one("#sb_files", Static).update(
                "\n".join(lines) if lines else "None"
            )
        except Exception:
            pass
        if event.diff:
            existing = [
                w
                for w in self.query(SideBySideDiff)
                if getattr(w, "_path", None) == event.file_path
            ]
            if not existing:
                artifact = AgentArtifact(
                    content=event.diff, title=event.file_path, kind="diff"
                )
                await self._mount_chat_widget(artifact)

    @on(TaskEscalatedEvent)
    async def handle_task_escalated(self, event: TaskEscalatedEvent) -> None:
        logger.warning(f"Task escalated: {event.task_id} - {event.reason}")
        widget = Static(
            f"[bold #ff5555]Escalation:[/] Task {event.task_id} — {event.reason} (retry {event.retry_count})",
            classes="error_msg",
            markup=True,
        )
        await self._mount_chat_widget(widget)

    @on(ContextDegradedEvent)
    async def handle_context_degraded(self, event: ContextDegradedEvent) -> None:
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

    @on(RetryAttemptEvent)
    async def handle_retry_attempt(self, event: RetryAttemptEvent) -> None:
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

    @on(RetrySucceededEvent)
    async def handle_retry_succeeded(self, event: RetrySucceededEvent) -> None:
        logger.info(f"Retry succeeded on attempt {event.attempt_number}")
        prov = f"  [{event.provider}]" if event.provider else ""
        w = Static(
            f"[bold #22c55e]✓ Retry succeeded[/] on attempt {event.attempt_number}{prov}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)

    @on(RetryFailedEvent)
    async def handle_retry_failed(self, event: RetryFailedEvent) -> None:
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

    # ── Input handling: ChatTextArea ──────────────────────────────────────

    @on(ChatTextArea.Submitted)
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        raw_val = event.text.strip()
        if not raw_val:
            return

        # Clear input immediately; dismiss palette + file picker
        event.text_area.clear()
        event.text_area.history_index = -1
        self._palette_active = False
        self._palette_matches = []
        self._at_picker_hide()

        # Slash commands (always allowed, even while running)
        if raw_val.startswith("/"):
            parts = raw_val[1:].split(" ", 1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            self.post_message(SlashCommand(command=cmd, args=args))
            return

        # Block user messages while agent is running (§12.5)
        if self.agent_running:
            self.notify("Agent is running — use /interrupt to stop", severity="warning")
            return

        # Expand @path tokens to inline file content
        val = self._expand_at_tokens(raw_val)

        display_val = val if len(val) <= 200 else val[:200] + f"… ({len(val)} chars)"
        logger.info(f"User prompt: {val[:80]}")

        # Update frecency history and reload into input widget
        self._bridge.update_prompt_history(raw_val)
        try:
            inp = self.query_one("#user_input", ChatTextArea)
            inp._prompt_history = self._bridge.load_prompt_history()
        except Exception:
            pass

        # Task status panel
        task_text = val[:80] + ("…" if len(val) > 80 else "")
        try:
            self.query_one("#sb_task_status", Static).update(task_text)
        except Exception:
            pass

        # Show in chat
        widget = Label(
            f"[bold #3b82f6]You:[/] {display_val}", classes="user_msg", markup=True
        )
        await self._mount_chat_widget(widget)

        # Route to bridge
        self._last_task_text = val
        sent = self._bridge.send_prompt(val)
        if not sent:
            self.notify("Agent already running", severity="warning")

    @on(ChatTextArea.TextChanged)
    def on_chat_text_area_changed(self, event: ChatTextArea.TextChanged) -> None:
        """Drive inline palette and @file picker from every text change."""
        text = event.text

        # ── Inline slash-command palette ─────────────────────────────────
        # LOW-13 fix: the old guard `not "\n" in text` prevented the palette
        # from appearing when the user typed a multiline message that *starts*
        # with a slash command.  Only the first line needs to start with "/" —
        # subsequent lines are arguments/context.
        first_line = text.split("\n", 1)[0]
        if first_line.startswith("/"):
            from .components.chat_input import SLASH_COMMANDS

            matches = [c for c in SLASH_COMMANDS if c.startswith(first_line.rstrip())]
            if matches:
                self._palette_active = True
                self._palette_matches = matches
                self._palette_index = 0
                return
        # Dismiss palette if text no longer starts with /
        if self._palette_active:
            self._palette_active = False
            self._palette_matches = []

        # ── @file picker ─────────────────────────────────────────────────
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

    # ── Interrupt signal from HistoryInput (kept for compat) ──────────────

    @on(HistoryInput.InterruptSignal)
    def handle_interrupt(self, event: HistoryInput.InterruptSignal) -> None:
        logger.warning("Double-Esc interrupt signal received")
        self._bridge.force_interrupt()
        self._finalize_stream()
        self.notify("Agent interrupted", severity="warning")
        self.post_message(AgentInterrupt())

    # ── Slash commands (§11) ──────────────────────────────────────────────

    @on(SlashCommand)
    async def handle_slash_command(self, event: SlashCommand) -> None:
        cmd = event.command.lower()
        args = event.args.strip()
        logger.info(f"Slash command: /{cmd} {args}")

        if cmd == "help":
            w = Static(SLASH_HELP, classes="system_msg help_msg", markup=False)
            await self._mount_chat_widget(w)

        elif cmd == "clear":
            self._clear_chat_panel()
            self.notify("Chat cleared")

        elif cmd in ("new", "reset"):
            self._save_session_snapshot()
            self._bridge.clear_history()
            self._clear_chat_panel()
            self._reset_sidebar()
            self._bridge.start_new_session()
            self.notify("New session started")

        elif cmd == "compact":
            self.notify("Compacting context…", severity="information")
            compacted = self._bridge.compact_context()
            msg = (
                "Context compacted — context window freed."
                if compacted
                else "Context compacted (mock)."
            )
            w = Static(f"[dim]{msg}[/]", classes="system_msg", markup=True)
            await self._mount_chat_widget(w)

        elif cmd == "continue":
            if not self._last_task_text:
                self.notify("No previous task to continue", severity="warning")
            else:
                self._bridge.restore_and_continue(
                    self._last_task_text, self._continue_state
                )
                if not self._bridge.is_running():
                    self.notify("No previous task to continue", severity="warning")

        elif cmd == "interrupt":
            self._bridge.interrupt()
            self.notify("Interrupt signal sent")

        elif cmd == "status":
            st = self._bridge.get_status()
            w = Static(
                f"[bold]Status[/]\n"
                f"  Running:   {'yes' if self.agent_running else 'no'}\n"
                f"  Task ID:   {st['task_id']}\n"
                f"  Role:      {self.active_role}\n"
                f"  Tokens:    {self.total_tokens:,} / {self.context_window:,}\n"
                f"  History:   {st['history_len']} messages\n"
                f"  WorkDir:   {st['working_dir']}",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)

        elif cmd == "fast":
            await self._slash_fast()

        elif cmd == "provider":
            await self._slash_provider(args)

        elif cmd == "model":
            await self._slash_model(args)

        elif cmd == "settings":
            self.action_open_settings()

        elif cmd == "sessions":
            from .screens.session_list import SessionListScreen

            self.push_screen(SessionListScreen())

        elif cmd == "timeline":
            from .screens.timeline import TimelineScreen

            self.push_screen(TimelineScreen(self._bridge.history))

        elif cmd == "diff":
            await self._slash_diff()

        elif cmd == "fork":
            await self._slash_fork()

        elif cmd == "mcp":
            await self._slash_mcp(args)

        elif cmd == "quit":
            self.action_quit_app()

        else:
            # Unrecognised: pass as plain text to agent
            if not self.agent_running:
                val = (
                    event.command if not event.args else f"{event.command} {event.args}"
                )
                widget = Label(
                    f"[bold #3b82f6]You:[/] /{val}",
                    classes="user_msg",
                    markup=True,
                )
                await self._mount_chat_widget(widget)
                self._bridge.send_prompt(f"/{val}")
            else:
                self.notify(f"Unknown command: /{cmd}", severity="warning")

    async def _slash_fast(self) -> None:
        """S8-C: /fast — switch to the smallest/fastest configured model (NANO tier)."""
        try:
            result = self._bridge.get_fast_model()
            nano_model = result.get("model") if result else None
            if not nano_model:
                all_models = self._settings.get_all_models_flat()
                for m in all_models:
                    name_lower = m.get("model", "").lower()
                    if any(kw in name_lower for kw in ("nano", "tiny", "1b", "3b")):
                        nano_model = m["model"]
                        break
            if not nano_model:
                nano_model = self._settings.get("default_model", "")

            if nano_model:
                self._settings.set("default_model", nano_model)
                self._settings.save()
                self._bridge.publish(
                    "model.routing",
                    {
                        "provider": self._settings.get("default_provider", ""),
                        "selected": nano_model,
                        "model_tier": "nano",
                    },
                )
                try:
                    self.query_one("#sb_model_info", Static).update(nano_model)
                except Exception:
                    pass
                self.notify(f"Fast mode: {nano_model} (NANO tier)")
            else:
                self.notify(
                    "No fast model configured — add model_routing.nano_model to config",
                    severity="warning",
                )
        except Exception as exc:
            self.notify(f"/fast error: {exc}", severity="warning")

    async def _slash_provider(self, args: str) -> None:
        providers = self._settings.available_providers
        if not providers:
            w = Static("No providers configured.", classes="system_msg")
            await self._mount_chat_widget(w)
            return
        if not args:
            lines = [
                f"  {i + 1}. {p.get('name', p)} ({p.get('type', '?')})"
                if isinstance(p, dict)
                else f"  {i + 1}. {p}"
                for i, p in enumerate(providers)
            ]
            w = Static("Providers:\n" + "\n".join(lines), classes="system_msg")
            await self._mount_chat_widget(w)
        else:
            target: Optional[dict] = None
            if args.isdigit():
                idx = int(args) - 1
                if 0 <= idx < len(providers):
                    target = providers[idx]
            else:
                q = args.lower()
                for p in providers:
                    pname = p.get("name", "").lower()
                    if q == pname or pname.startswith(q) or q in pname:
                        target = p
                        break
            if target is None:
                self.notify(f"Provider not found: {args}", severity="warning")
                return
            pname = target.get("name", args)
            pid = pname.lower().replace(" ", "_")
            role = self.active_role
            self._settings.set(f"{role}_provider", pid)
            self._settings.set("default_provider", pid)
            self._settings.save()
            first_model = (target.get("models") or [""])[0]
            try:
                self.query_one("#sb_provider", Static).update(pid)
                banner = self.query_one("#provider_banner", Static)
                banner.update(f"  {pid}  ·  {first_model}")
                banner.remove_class("connected", "error")
                banner.add_class("connected")
            except Exception:
                pass
            self._bridge.publish(
                "model.routing", {"provider": pid, "selected": first_model}
            )
            self.notify(f"Switched to provider: {pname}")

    async def _slash_model(self, args: str) -> None:
        all_models = self._settings.get_all_models_flat()
        if not args:
            if not all_models:
                current = self._settings.get("default_model", "—")
                w = Static(f"Current model: {current}", classes="system_msg")
            else:
                lines = [
                    f"  {i + 1}. {m['provider_name']}: {m['model']}"
                    for i, m in enumerate(all_models)
                ]
                current = self._settings.get("default_model", "—")
                w = Static(
                    f"Current: {current}\nAll models:\n" + "\n".join(lines),
                    classes="system_msg",
                )
            await self._mount_chat_widget(w)
        else:
            target_model: Optional[str] = None
            target_provider: Optional[str] = None
            if args.isdigit():
                idx = int(args) - 1
                if 0 <= idx < len(all_models):
                    target_model = all_models[idx]["model"]
                    target_provider = (
                        all_models[idx]["provider_name"].lower().replace(" ", "_")
                    )
            else:
                q = args.lower()
                for m in all_models:
                    if q == m["model"].lower() or q in m["model"].lower():
                        target_model = m["model"]
                        target_provider = m["provider_name"].lower().replace(" ", "_")
                        break
            if target_model is None:
                target_model = args
            role = self.active_role
            self._settings.set(f"{role}_model", target_model)
            self._settings.set("default_model", target_model)
            if target_provider:
                self._settings.set(f"{role}_provider", target_provider)
                self._settings.set("default_provider", target_provider)
            self._settings.save()
            try:
                self.query_one("#sb_model_info", Static).update(target_model)
                if target_provider:
                    self.query_one("#sb_provider", Static).update(target_provider)
                    provider_id = target_provider
                    banner = self.query_one("#provider_banner", Static)
                    banner.update(f"  {provider_id}  ·  {target_model}")
            except Exception:
                pass
            self._bridge.publish(
                "model.routing",
                {
                    "provider": target_provider
                    or self._settings.get("default_provider", ""),
                    "selected": target_model,
                },
            )
            self.notify(f"Model switched to: {target_model}")

    async def _slash_mcp(self, args: str) -> None:
        """S3-C: /mcp [list|add <name> <cmd…>|status] — manage MCP servers."""
        try:
            from src.core.config_loader import (
                get_mcp_config,
                get_mcp_servers,
                load_merged_config,
            )
            from pathlib import Path as _Path
            import json as _json

            sub = args.strip().split(None, 1)
            subcmd = sub[0].lower() if sub else "list"
            rest = sub[1] if len(sub) > 1 else ""

            if subcmd in ("list", ""):
                servers = get_mcp_servers()
                if not servers:
                    text = "[dim]No MCP servers configured.[/]\n\nAdd one with: [bold]/mcp add <name> <cmd>[/]"
                else:
                    lines = ["[bold]Configured MCP servers:[/]"]
                    for s in servers:
                        name = s.get("name", "?")
                        cmd = " ".join(s.get("cmd", [])) or "(no cmd)"
                        auto = (
                            "auto" if s.get("auto_register_tools", True) else "manual"
                        )
                        lines.append(f"  • [bold]{name}[/] — {cmd}  [{auto}]")
                    text = "\n".join(lines)
                w = Static(text, classes="system_msg", markup=True)
                await self._mount_chat_widget(w)

            elif subcmd == "add":
                # /mcp add <name> <cmd part1> [part2 …]
                parts = rest.split()
                if len(parts) < 2:
                    self.notify(
                        "Usage: /mcp add <name> <cmd> [args…]", severity="warning"
                    )
                    return
                new_name = parts[0]
                new_cmd = parts[1:]
                # Write into workspace .agent/config.json
                agent_dir = (
                    _Path(
                        getattr(
                            getattr(self._bridge, "_orchestrator", None),
                            "working_dir",
                            ".",
                        )
                    )
                    / ".agent"
                )
                agent_dir.mkdir(parents=True, exist_ok=True)
                config_path = agent_dir / "config.json"
                cfg: dict = {}
                if config_path.exists():
                    try:
                        cfg = _json.loads(config_path.read_text(encoding="utf-8"))
                    except Exception:
                        cfg = {}
                mcp_section = cfg.setdefault("mcp", {})
                servers_list: list = mcp_section.setdefault("servers", [])
                # Replace existing entry with same name
                servers_list = [s for s in servers_list if s.get("name") != new_name]
                servers_list.append(
                    {"name": new_name, "cmd": new_cmd, "auto_register_tools": True}
                )
                mcp_section["servers"] = servers_list
                cfg["mcp"] = mcp_section
                config_path.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")
                text = (
                    f"[bold]MCP server added:[/] {new_name}\n"
                    f"  cmd: {' '.join(new_cmd)}\n"
                    f"  Config saved to [dim]{config_path}[/]\n\n"
                    f"Restart the agent session to connect to the new server."
                )
                w = Static(text, classes="system_msg", markup=True)
                await self._mount_chat_widget(w)
                self.notify(f"MCP server '{new_name}' added")

            elif subcmd == "status":
                # Show live connection status via the MCP chip label
                chip = self.query_one("#mcp_status_chip", Static)
                chip_text = str(chip.renderable) if chip else "(none)"
                orch = getattr(self._bridge, "_orchestrator", None)
                clients = getattr(orch, "_mcp_clients", {}) if orch else {}
                if clients:
                    lines = ["[bold]MCP server status:[/]"]
                    for name, client in clients.items():
                        connected = getattr(client, "_connected", False)
                        state = (
                            "[green]connected[/]"
                            if connected
                            else "[red]disconnected[/]"
                        )
                        lines.append(f"  • [bold]{name}[/] — {state}")
                    text = "\n".join(lines)
                else:
                    text = (
                        "[dim]No active MCP connections.[/]\n\nStatus chip: "
                        + chip_text
                    )
                w = Static(text, classes="system_msg", markup=True)
                await self._mount_chat_widget(w)

            else:
                self.notify(
                    f"Unknown /mcp subcommand: {subcmd}. Use list, add, or status.",
                    severity="warning",
                )
        except Exception as exc:
            self.notify(f"/mcp error: {exc}", severity="error")

    async def _slash_diff(self) -> None:
        """Show working-directory diff since the last git snapshot."""
        try:
            orch = self._bridge._orchestrator  # type: ignore[attr-defined]
            snap_mgr = getattr(orch, "snapshot_manager", None) if orch else None
            if snap_mgr is None:
                from src.core.orchestration.snapshot_manager import GitSnapshotManager  # type: ignore[import]
                from pathlib import Path as _Path

                _workdir = _Path(getattr(orch, "working_dir", ".") if orch else ".")
                snap_mgr = GitSnapshotManager(
                    workspace=_workdir,
                    project_id=_workdir.name or "default",
                )

            # Get the diff against the earliest session snapshot (S4-A / S5-C).
            # AgentState.snapshots holds tree hashes; first entry = session start.
            import asyncio as _asyncio

            _base_hash: str = "HEAD"
            try:
                _state = getattr(orch, "_last_agent_state", None) if orch else None
                _snaps = (_state or {}).get("snapshots") or []
                if _snaps:
                    _base_hash = _snaps[0]
            except Exception:
                pass

            diff_text: str = ""
            try:
                diff_text = await _asyncio.wait_for(
                    snap_mgr.diff(_base_hash), timeout=5.0
                )
            except Exception:
                diff_text = ""

            if not diff_text or not diff_text.strip():
                diff_text = "(no changes since last snapshot)"

            w = Static(
                f"[bold]Working-directory diff[/]\n{diff_text}",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
        except Exception as exc:
            self.notify(f"diff failed: {exc}", severity="error")

    async def _slash_fork(self) -> None:
        """Fork the current session to a new independent copy."""
        try:
            orch = self._bridge._orchestrator  # type: ignore[attr-defined]
            store = getattr(orch, "session_store", None) if orch else None
            current_id: str = getattr(orch, "_current_task_id", "") or ""

            if store is None or not current_id:
                self.notify("No active session to fork", severity="warning")
                return

            fork_id = store.fork_session(current_id)
            w = Static(
                f"[bold]Session forked[/]\n"
                f"  Source:  {current_id}\n"
                f"  Fork ID: {fork_id}\n\n"
                f"The forked session is an independent copy.  Switch to it with "
                f"[bold]/sessions[/] → select the fork.",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            self.notify(f"Session forked: {fork_id[:12]}…")
        except Exception as exc:
            self.notify(f"fork failed: {exc}", severity="error")

    # ── Palette / settings / connect ──────────────────────────────────────

    @on(PaletteCommand)
    def handle_palette_command(self, event: PaletteCommand) -> None:
        cmd = event.command_id
        logger.info(f"Palette command: {cmd}")
        dispatch = {
            "action_toggle_console": self.action_toggle_console,
            "action_open_settings": self.action_open_settings,
            "action_toggle_sidebar": self.action_toggle_sidebar,
            "action_quit": self.action_quit_app,
            "action_new_session": lambda: self.post_message(SlashCommand("new")),
            "action_check_providers": lambda: self.notify(
                "Provider check not connected"
            ),
            "action_compact_memory": lambda: self.post_message(SlashCommand("compact")),
        }
        fn = dispatch.get(cmd)
        if fn:
            fn()
        else:
            logger.debug(f"Unhandled palette command: {cmd}")

    @on(ConnectProvider)
    def handle_connect_provider(self, event: ConnectProvider) -> None:
        from .features.settings.screen import ProviderConfigScreen

        prov = self._settings.get_provider_by_id(event.provider_id)
        name = prov["name"] if prov else event.provider_id.replace("_", " ").title()
        self.push_screen(ProviderConfigScreen(event.provider_id, name))

    @on(SaveProviderCredentials)
    def handle_save_credentials(self, event: SaveProviderCredentials) -> None:
        logger.info(f"Save provider credentials: {event.provider_id}")
        try:
            from src.ui.config_writer import save_provider_credentials

            save_provider_credentials(
                provider_id=event.provider_id,
                api_key=event.api_key,
                base_url=getattr(event, "base_url", None),
            )
            self.notify(
                f"Credentials for {event.provider_id} saved", severity="information"
            )
        except Exception as exc:
            logger.error(f"Failed to persist credentials: {exc}")
            self.notify(f"Error saving credentials: {exc}", severity="error")

    @on(UpdateRoleModel)
    def handle_update_role_model(self, event: UpdateRoleModel) -> None:
        logger.info(f"Role model update: {event.role} → {event.model_id}")

    @on(UpdateSettings)
    def handle_update_settings(self, event: UpdateSettings) -> None:
        logger.info(f"Settings updated: {list(event.updates.keys())}")
        if "context_window" in event.updates:
            try:
                self.context_window = int(event.updates["context_window"])
            except (ValueError, TypeError):
                pass
        if "theme" in event.updates:
            try:
                self.theme = event.updates["theme"]
            except Exception:
                pass
        self._update_status_bar()

    # ── Actions ───────────────────────────────────────────────────────────

    def action_toggle_mode(self) -> None:
        self._role_idx = (self._role_idx + 1) % len(self._role_cycle)
        new_role = self._role_cycle[self._role_idx]
        self.active_role = new_role
        self._update_role_display(new_role)
        self._update_status_bar()
        self.notify(f"Switched to {ROLE_LABELS.get(new_role, new_role)}")

    def action_toggle_console(self) -> None:
        try:
            console = self.query_one("#console_panel", ConsolePanel)
            if "hidden" in console.classes:
                console.remove_class("hidden")
            else:
                console.add_class("hidden")
        except Exception as e:
            logger.error(f"Error toggling console: {e}")

    def action_toggle_sidebar(self) -> None:
        try:
            sidebar = self.query_one("#right_sidebar", VerticalScroll)
            if "hidden" in sidebar.classes:
                sidebar.remove_class("hidden")
            else:
                sidebar.add_class("hidden")
        except Exception as e:
            logger.error(f"Error toggling sidebar: {e}")

    def action_show_commands(self) -> None:
        from .features.palette.screen import CommandPalette

        self.push_screen(CommandPalette(self._settings))

    def action_open_model_picker(self) -> None:
        """TUI-T12: Ctrl+M — jump directly to the model selection submenu."""
        from .features.palette.screen import CommandPalette

        self.push_screen(
            CommandPalette(self._settings, initial_action="menu_switch_model")
        )

    def action_open_settings(self) -> None:
        from .features.settings.screen import SettingsScreen

        self.push_screen(SettingsScreen(self._settings))

    # Legacy /clear helper alias
    def _clear_session(self) -> None:
        self._clear_chat_panel()
        self._reset_sidebar()
        self.notify("Session cleared")
