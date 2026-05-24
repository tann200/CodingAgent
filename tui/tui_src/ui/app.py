"""
AgentApp — TUI System Specification v2.0 compliant Textual application.
Wires exclusively through AgentBridge → EventBus; never imports src.core directly.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from textual.notifications import SeverityLevel
    from .components import ConsolePanel, FilePickerOverlay, StreamView

from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Header, Static, Label, Input, Button
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual import on
from textual.reactive import reactive

from .components import (
    ChatTextArea,
    HistoryInput,
    SideBySideDiff,
    SubagentProgress,
)
from .components.status_bar import StatusBarMixin, ROLE_LABELS as _SB_ROLE_LABELS, ROLE_COLORS as _SB_ROLE_COLORS
from .components.chat_mixin import ChatDisplayMixin
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
    # GitHub Copilot OAuth device flow
    DeviceFlowCompleteEvent,
    DeviceFlowErrorEvent,
    # Subagent visibility (SUBAGENT-VIS-2)
    SubagentStartEvent,
    SubagentFinishEvent,
    # TASK-TUI-9: compaction divider
    ContextCompactedEvent,
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
    StartGithubDeviceFlow,
)

logger = get_logger("app")

# LOW-11 fix: extract cost-rate constants so they are easy to update and
# shared between both token-event handlers (previously each handler had its
# own hardcoded formula, making them inconsistent).
# Rates are per-1 000 tokens in USD (adjust as model pricing changes).
_COST_INPUT_PER_1K: float = 0.001  # input / system / task tokens
_COST_OUTPUT_PER_1K: float = 0.003  # output tokens

ROLE_LABELS = _SB_ROLE_LABELS
ROLE_COLORS = _SB_ROLE_COLORS

SLASH_HELP = """\
Available commands:
  /help          — show this list
  /clear         — clear chat output (keeps history)
  /new  /reset   — new session (clears history)
  /compact       — compact conversation context
  /undo          — undo last user message (removes it from history)
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
  /share         — export conversation to a markdown file (clipboard if pyperclip available)
  /rename <name> — rename the current session
  /worktree [list|create|remove <id>] — manage git worktree isolation
  /mcp [list|add <name> <cmd…>|status] — manage MCP servers
  /quit          — exit the application"""

SAFE_SLASH_CMDS = {"interrupt", "status", "help", "quit"}

# Max chars of a single @-referenced file inlined into prompt
_AT_FILE_MAX_CHARS = 8000
_MAX_CHAT_WIDGETS = 200  # prune chat log when it exceeds this many widgets
# Workspace dirs to skip when scanning files for @ picker
_AT_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}

# ── File-path loaders for real src.core modules ───────────────────────────────
# In the TUI bootstrap, sys.modules['src'] is remapped to tui/src, so
# ``from src.core.*`` fails.  We load these by absolute path instead, exactly
# as core_bridge.py does for github_copilot_auth.
# app.py lives at  tui/src/ui/app.py  →  parents[3] == project root.


def _load_llm_manager_module():
    """Return the real src.core.inference.llm_manager module, cached in sys.modules."""
    import importlib.util
    import sys as _sys

    _MOD_NAME = "_llm_manager_real"
    if _MOD_NAME in _sys.modules:
        return _sys.modules[_MOD_NAME]
    _path = Path(__file__).parents[3] / "src" / "core" / "inference" / "llm_manager.py"
    spec = importlib.util.spec_from_file_location(_MOD_NAME, str(_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load llm_manager from {_path}")
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[_MOD_NAME] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    except Exception:
        _sys.modules.pop(_MOD_NAME, None)
        raise
    return mod


def _load_config_loader_module():
    """Return the real src.core.config_loader module, cached in sys.modules."""
    import importlib.util
    import sys as _sys

    _MOD_NAME = "_config_loader_real"
    if _MOD_NAME in _sys.modules:
        return _sys.modules[_MOD_NAME]
    _path = Path(__file__).parents[3] / "src" / "core" / "config_loader.py"
    spec = importlib.util.spec_from_file_location(_MOD_NAME, str(_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load config_loader from {_path}")
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[_MOD_NAME] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    except Exception:
        _sys.modules.pop(_MOD_NAME, None)
        raise
    return mod


def _load_copilot_auth_module():
    """Return the real src.core.inference.adapters.github_copilot_auth module, cached."""
    import importlib.util
    import sys as _sys

    _MOD_NAME = "_copilot_auth_real"
    if _MOD_NAME in _sys.modules:
        return _sys.modules[_MOD_NAME]
    _path = (
        Path(__file__).parents[3]
        / "src"
        / "core"
        / "inference"
        / "adapters"
        / "github_copilot_auth.py"
    )
    spec = importlib.util.spec_from_file_location(_MOD_NAME, str(_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load github_copilot_auth from {_path}")
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[_MOD_NAME] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    except Exception:
        _sys.modules.pop(_MOD_NAME, None)
        raise
    return mod


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


# GAP-TUI-1: Per-tool icons matching OpenCode's InlineTool icons.
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

# Status icons for TodoWrite items (OpenCode-compatible)
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


def _render_todo_block(args: dict, result_text: str) -> str:
    """GAP-TUI-4: Render todowrite / manage_todo call as a '# Todos' block.

    Handles two arg shapes:
      • OpenCode style: args['todos'] = [{content, status, priority}, …]
      • Local manage_todo style: parse result_text for step lines
    Returns Rich markup string, or '' to fall back to generic rendering.
    """
    # OpenCode-style todos list
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

    # manage_todo: parse markdown result text for checkbox lines
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

    # Fallback: show action + brief result
    action = args.get("action", "")
    if action:
        return f"[bold #22c55e]⚙ Todo {action}[/]"
    return ""


def _render_question_block(args: dict, result_text: str) -> str:
    """GAP-TUI-5: Render question / ask_user call as a '# Questions' Q&A block.

    Handles:
      • OpenCode style: args['questions'] = [{question, header, options}, …]
      • Local ask_user style: args['question'] (str), result_text has 'answer'
    Returns Rich markup string, or '' to fall back to generic rendering.
    """
    import json as _json

    # OpenCode-style list of questions
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

    # Local ask_user style
    question = args.get("question", "")
    if question:
        lines = ["[bold]# Questions[/]", f"  [dim]{question}[/]"]
        # Parse answer from result JSON
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


class AgentApp(App[None], StatusBarMixin, ChatDisplayMixin):
    COMMAND_PALETTE = False
    BINDINGS = [
        ("tab", "toggle_mode", "agents"),
        ("ctrl+l", "toggle_console", "console"),
        ("ctrl+o", "show_commands", "commands"),
        ("ctrl+s", "open_settings", "settings"),
        ("ctrl+m", "open_model_picker", "model"),
        ("ctrl+q", "quit_app", "quit"),
        ("ctrl+r", "open_sessions", "sessions"),
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
        # Per-tool args cache (tool_id → args dict) for rich rendering at finish
        self._tool_args: dict[str, dict] = {}
        # Per-subagent in-progress spinner tracking (SUBAGENT-VIS-4)
        self._subagent_widgets: dict[str, "SubagentProgress"] = {}
        # Continue state for /continue command
        self._continue_state: Optional[dict] = None
        # Last task text for /continue
        self._last_task_text: str = ""
        # GAP-FOOTER-1: pending permission request count (bash + tool)
        self._pending_perm_count: int = 0
        # GAP-PERM-3: tool names that have been "allow always"ed this session
        self._allow_always_tools: set[str] = set()
        # TASK-TUI-9 / GAP-MSG-2: message queue for submissions during agent run.
        # Upgraded from single-slot to a proper deque so multiple quick submissions
        # are all delivered in order when the agent becomes idle.
        import collections as _collections

        # Use a non-conflicting name for the TUI's own queued messages so we
        # don't shadow Textual's internal _message_queue (which is a Queue).
        self._queued_messages: _collections.deque[str] = _collections.deque()
        self._queued_widget: Optional[Static] = None
        # Legacy alias kept for any code that still references _queued_message
        self._queued_message: Optional[str] = None
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

        # ── GitHub Copilot OAuth device-flow state ────────────────────────
        import threading as _threading

        self._oauth_screen: Optional[object] = None  # OAuthDeviceFlowScreen instance
        self._device_flow_cancel: _threading.Event = _threading.Event()

        logger.info("AgentApp initialized")

    # ── Layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        from .components import ConsolePanel, FilePickerOverlay

        yield Header(show_clock=False)
        yield Static("  connecting…", id="provider_banner")

        with Horizontal(id="main_workspace"):
            yield ConsolePanel(id="console_panel")

            with Vertical(id="left_column"):
                yield VerticalScroll(id="chat_log")

                # File picker overlay sits just above the input
                yield FilePickerOverlay(id="file_picker")

                yield ChatTextArea(
                    id="user_input",
                    placeholder="Type a message or /help … (Esc to interrupt)",
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

                # ── Active subagents ───────────────────────────────────────
                yield Label("SUBAGENTS", classes="sb_title")
                yield Static("none", id="sb_subagent_status")

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
            yield Static("", id="perm_count_chip", markup=True)
            yield Static("", id="mcp_status_chip", markup=True)
            # GAP-FOOTER-3: subagent navigation chip — shows active count + click-to-open
            yield Button("", id="subagent_footer_chip", variant="default")

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        from .components import ConsolePanel, FilePickerOverlay

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
        # BUG-FIX #4: drain queued messages on shutdown to prevent lost prompts
        if getattr(self, "_queued_messages", None):
            while self._queued_messages:
                try:
                    msg = self._queued_messages.popleft()
                    logger.warning("Draining queued message on shutdown: %s", msg[:60])
                    self._bridge.send_prompt(msg)
                except Exception as _drain_err:
                    logger.debug("shutdown drain send failed: %s", _drain_err)
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
    # _update_perm_badge, _update_role_display, _update_status_bar are
    # inherited from StatusBarMixin (tui/src/ui/components/status_bar.py).

    # _ensure_stream_widget, _mount_and_scroll, _finalize_stream → ChatDisplayMixin

    def _append_log_line(self, line: str, level: str = "INFO") -> None:
        """§16.4 — write log.new events DIRECTLY to console, never through logging."""
        try:
            from .components import ConsolePanel

            console = self.query_one("#console_panel", ConsolePanel)
            console.write_line(line, level)
        except Exception:
            pass

    # _prune_chat_log, _mount_chat_widget, _sched_chat_widget → ChatDisplayMixin

    # ── Session snapshot ──────────────────────────────────────────────────

    def _get_sessions_dir(self) -> Path:
        # Cross-platform sessions directory — prefer core.paths.get_sessions_dir()
        from ._core_paths_loader import get_sessions_dir as _get_sessions_dir_helper

        d = _get_sessions_dir_helper()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_session_snapshot(self) -> None:
        """Snapshot current session to the sessions directory returned by
        ``src.core.paths.get_sessions_dir()`` (fallback to ~/.coding_agent/sessions
        in TUI dev mode).

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

            # Ensure parent dir immediately before writing
            p.parent.mkdir(parents=True, exist_ok=True)

            # Prefer central atomic writer; fall back to mkstemp+replace
            try:
                from src.core.io_utils import atomic_write_json

                logger.debug("app: attempting atomic_write_json for %s", p)
                ok = atomic_write_json(p, payload, logger=logger)
                if ok:
                    logger.info("Session snapshot written atomically: %s", p)
                    return
                logger.warning(
                    "app: atomic_write_json returned False for %s; falling back",
                    p,
                )
            except Exception as _e:
                import traceback as _traceback

                logger.debug(
                    "app: atomic_write_json unavailable or failed for %s; falling back: %s\n%s",
                    p,
                    _e,
                    _traceback.format_exc(),
                )

            fd = None
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
                try:
                    try:
                        fobj = os.fdopen(fd, "w", encoding="utf-8")
                    except Exception:
                        if fd is not None:
                            os.close(fd)
                        raise
                    with fobj:
                        json.dump(payload, fobj, ensure_ascii=False)
                        try:
                            fobj.flush()
                            os.fsync(fobj.fileno())
                        except Exception:
                            pass
                    os.replace(tmp_path, str(p))
                except Exception:
                    try:
                        if fd is not None:
                            os.close(fd)
                    except Exception:
                        pass
                    raise
            except Exception as e:
                logger.error("Session snapshot save failed: %s", e)
                try:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception:
            pass

    # ── §10.3 New session ─────────────────────────────────────────────────

    def _handle_session_new(self) -> None:
        """Called from bridge when session.new fires."""
        self._clear_chat_panel()
        self._reset_sidebar()

    # _clear_chat_panel → ChatDisplayMixin

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

    # _list_workspace_files → ChatDisplayMixin

    # _at_picker_navigate, _at_picker_complete, _at_picker_hide → ChatDisplayMixin

    # ── Inline palette helpers ─────────────────────────────────────────────

    # _palette_navigate, _palette_complete → ChatDisplayMixin

    # ── @file token expansion ─────────────────────────────────────────────

    # _expand_at_tokens → ChatDisplayMixin

    # ── EventBus / bus event handlers ─────────────────────────────────────

    @on(RequestSystemSettings)
    def handle_request_system_settings(self, _: RequestSystemSettings) -> None:
        """Delegate system settings hydration to the bridge-owned startup path."""
        self._bridge._publish_system_settings()

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
            # Don't overwrite the provider banner here — provider status events
            # will update it with the real provider name.
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

        # TASK-TUI-9: drain the message queue when the agent becomes idle
        if not event.running and self._queued_messages:
            self.call_later(self._drain_message_queue)

        # MID-INJ: mid-run messages are now buffered by the bridge and injected
        # as system-reminders during the running execution — no post-idle flush needed.

    def _drain_message_queue(self) -> None:
        """TASK-TUI-9: Send queued messages in order now that agent is idle."""
        while self._queued_messages and not self.agent_running:
            msg = self._queued_messages.popleft()
            logger.info(f"Draining queued message: {msg[:60]}")
            self._bridge.send_prompt(msg)

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
        self._chat_handle_stream_chunk(event)

    @on(StreamingThinkingUpdate)
    def handle_thinking_update(self, event: StreamingThinkingUpdate) -> None:
        self._chat_handle_thinking_update(event)

    @on(DisplayReasoning)
    async def handle_reasoning(self, event: DisplayReasoning) -> None:
        await self._chat_handle_reasoning(event)

    @on(AgentFinalResponse)
    async def handle_final_response(self, event: AgentFinalResponse) -> None:
        await self._chat_handle_final_response(event)

    @on(WorkerError)
    async def handle_error(self, event: WorkerError) -> None:
        await self._chat_handle_error(event)

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
        # GAP-FOOTER-2: delegated to StatusBarMixin._update_mcp_status_chip
        self._update_mcp_status_chip(event.running, event.count, event.has_error)

    # ── Tool permission gate ───────────────────────────────────────────────

    @on(ToolPermissionEvent)
    async def handle_tool_permission(self, event: ToolPermissionEvent) -> None:
        logger.warning(f"Tool permission required: {event.tool}  id={event.tool_id}")
        # GAP-PERM-3: cache tool name by tool_id so the "Allow Always" handler can
        # look it up without fragile regex on a rendered Static widget.
        if not hasattr(self, "_perm_tool_names"):
            self._perm_tool_names: dict[str, str] = {}
        self._perm_tool_names[event.tool_id] = event.tool
        # GAP-PERM-3: if this tool was already "allow always"ed, auto-approve
        if event.tool in self._allow_always_tools:
            logger.info(f"Auto-approving {event.tool} (allow-always)")
            self._bridge.publish("tool.permission_granted", {"tool_id": event.tool_id})
            self.post_message(ToolPermissionApproved(tool_id=event.tool_id))
            return
        self._update_perm_badge(+1)
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
        # GAP-PERM-3: "Allow Always" remembers the tool name for this session
        await row.mount(
            Button("Allow Always", id=f"btn_tool_perm_always_{tid}", variant="warning")
        )
        chat_log.scroll_end(animate=False)
        self._prune_chat_log()

    # ── Per-turn usage summary (TUI-T6) ──────────────────────────────────

    @on(UsageTurnSummaryEvent)
    def handle_usage_turn_summary(self, event: UsageTurnSummaryEvent) -> None:
        self._chat_handle_usage_turn_summary(event)

    @on(DoomLoopEvent)
    async def handle_doom_loop(self, event: DoomLoopEvent) -> None:
        await self._chat_handle_doom_loop(event)

    # ── Tool call 3-beat lifecycle (§6.1) ─────────────────────────────────

    @on(ToolCallStartEvent)
    def handle_tool_start(self, event: ToolCallStartEvent) -> None:
        logger.info(f"Tool start: {event.tool_name}  id={event.tool_id}")
        self._tool_call_count += 1
        icon = _TOOL_ICONS.get(event.tool_name.lower(), "⠿")

        # Cache args for rich rendering at finish time (GAP-TUI-4/5)
        if event.tool_id:
            self._tool_args[event.tool_id] = event.tool_args

        # GAP-TUI-3: bash tool gets a fenced-block style pending label.
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
    def handle_tool_finish(self, event: ToolCallFinishEvent) -> None:
        logger.info(f"Tool finish: {event.tool_name}  ok={event.ok}")
        icon = _TOOL_ICONS.get(event.tool_name.lower(), "✓" if event.ok else "✗")
        ok_icon = "✓" if event.ok else "✗"
        color = "#22c55e" if event.ok else "#ff5555"
        result_lines = event.result_text.strip().splitlines()

        # Retrieve cached args (for rich rendering of todowrite/question).
        cached_args = self._tool_args.pop(event.tool_id, {}) if event.tool_id else {}

        # TASK-TUI-4: todowrite / manage_todo → render as TodoListWidget
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

        # GAP-TUI-5: question / ask_user → render as # Questions Q&A block.
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

        # GAP-TUI-6: task/delegation → render summary with toolcall count
        if tool_lower in ("task", "delegation") and event.ok:
            role = cached_args.get("role") or cached_args.get("agent_type") or "Agent"
            task_desc = cached_args.get("task") or cached_args.get("description") or ""
            task_short = task_desc[:60] + ("…" if len(task_desc) > 60 else "")
            # Count tool calls mentioned in result
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

        # TASK-TUI-3: Use BashBlock for collapsible bash output
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

        # Generic: truncate long output
        if len(result_lines) > 60:
            extra = len(result_lines) - 60
            result_lines = result_lines[:60] + [f"… {extra} more lines"]
        result_display = "\n".join(result_lines)
        label = f"{icon} {event.tool_name}"

        sep = "\n" if result_display else ""
        if event.tool_id and event.tool_id in self._tool_widgets:
            w = self._tool_widgets.pop(event.tool_id)
            self.call_later(
                lambda widget=w, ic=ok_icon, col=color, r=result_display, lbl=label, s=sep: (
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

    # ── Subagent lifecycle (SUBAGENT-VIS-4) ──────────────────────────────

    def _update_subagent_footer(self) -> None:
        """GAP-FOOTER-3: keep the footer chip in sync with active subagent count."""
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
    async def handle_subagent_clicked(self, event: SubagentProgress.Clicked) -> None:
        """Open the child session detail screen when user clicks a finished subagent."""
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
    def handle_subagent_start(self, event: SubagentStartEvent) -> None:
        logger.info(f"Subagent start: {event.role}  id={event.child_session_id}")
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
    def handle_subagent_finish(self, event: SubagentFinishEvent) -> None:
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
        # GAP-TUI-2 / GAP-CONFIG-1: choose renderer based on diff_style setting
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
    async def handle_diff_accepted(self, event: SideBySideDiff.Accepted) -> None:
        logger.info(f"Diff accepted: {event.path}")
        w = Static(
            f"[bold #22c55e]✓ Accepted:[/] {event.path}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)
        # PREV-1: Resolve the preview gate so file_tools can proceed with the write.
        try:
            self._bridge.confirm_file_preview(event.path)
        except Exception:
            pass

    @on(SideBySideDiff.Rejected)
    async def handle_diff_rejected(self, event: SideBySideDiff.Rejected) -> None:
        logger.info(f"Diff rejected: {event.path}")
        w = Static(
            f"[bold #ff5555]✗ Rejected:[/] {event.path}",
            classes="retry_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)
        # PREV-1: Resolve the preview gate so file_tools can abort the write.
        try:
            self._bridge.reject_file_preview(event.path)
        except Exception:
            pass

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
        await self._chat_handle_plan_requested(event)

    # ── Bash tier-3 approval gate (§16.1) ─────────────────────────────────

    @on(BashApprovalEvent)
    async def handle_bash_approval(self, event: BashApprovalEvent) -> None:
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
    async def on_any_button(self, event: Button.Pressed) -> None:
        """Single handler for all buttons — avoids double-dispatch."""
        btn_id = event.button.id or ""

        # Determine whether this handler owns the button.  Only stop propagation
        # when we actually handle it; otherwise child-widget button handlers
        # (settings panel, modal screens) would never fire. (TUI-M1)
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

        # GAP-FOOTER-3: subagent footer chip — open SessionScreen filtered to subagents
        if btn_id == "subagent_footer_chip":
            from .screens.session_screen import SessionScreen

            self.push_screen(SessionScreen(filter_subagents=True))
            return

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
            # GAP-MSG-3: mark in-progress tool widget as denied (warning color)
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

        # ── Tool permission approval ────────────────────────────────────
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
            # GAP-MSG-3: mark in-progress tool widget as denied (warning color)
            if tool_id in self._tool_widgets:
                w_pending = self._tool_widgets.pop(tool_id)
                self.call_later(
                    lambda wp=w_pending: wp.update(
                        "[bold #ff5555 strike]✗ tool — denied[/]"
                    )
                )
            # GAP-PERM-2: offer a feedback input so the agent knows why it was denied
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

        # GAP-PERM-3: Allow Always — approve now + register tool for auto-approval + persist
        elif btn_id.startswith("btn_tool_perm_always_"):
            tool_id = btn_id[len("btn_tool_perm_always_") :]
            self._update_perm_badge(-1)
            self._bridge.publish("tool.permission_granted", {"tool_id": tool_id})
            try:
                perm_row = self.query_one(f"#tool_perm_{tool_id}")
                # Look up the tool name from the cache populated at permission-request time.
                _perm_names = getattr(self, "_perm_tool_names", {})
                tool_name = _perm_names.get(tool_id, tool_id)
                self._allow_always_tools.add(tool_name)
                # GAP-PERM-3: persist the "allow always" rule to PermissionPolicy
                try:
                    from src.core.orchestration.permission_policy import (
                        PermissionRule,
                        Behavior,
                        get_permission_policy,
                    )

                    policy = get_permission_policy()
                    # Add a rule that allows this tool always (priority: append to end)
                    policy.add_rule(
                        PermissionRule(pattern=tool_name, behavior=Behavior.ALLOW)
                    )
                    policy.save()  # Persist to user permissions file (see src.core.paths.get_permissions_path())
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
                f"In: {event.system + event.task + event.tools:,} | Out: {self._session_output_tokens:,}"
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
        await self._chat_handle_session_health(event)

    # ── Status + role ─────────────────────────────────────────────────────

    @on(StatusUpdate)
    def handle_status(self, event: StatusUpdate) -> None:
        logger.info(f"Status: {event.message}")
        self._update_status_text(event.message)
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
        self._update_provider_status_widgets(event.provider, event.new_status)
        # Update banner CSS class for visual connected/error state
        try:
            banner = self.query_one("#provider_banner", Static)
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
                from .components import AgentArtifact

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

    @on(ContextCompactedEvent)
    async def handle_context_compacted(self, event: ContextCompactedEvent) -> None:
        await self._chat_handle_context_compacted(event)

    @on(ContextDegradedEvent)
    async def handle_context_degraded(self, event: ContextDegradedEvent) -> None:
        await self._chat_handle_context_degraded(event)

    @on(RetryAttemptEvent)
    async def handle_retry_attempt(self, event: RetryAttemptEvent) -> None:
        await self._chat_handle_retry_attempt(event)

    @on(RetrySucceededEvent)
    async def handle_retry_succeeded(self, event: RetrySucceededEvent) -> None:
        await self._chat_handle_retry_succeeded(event)

    @on(RetryFailedEvent)
    async def handle_retry_failed(self, event: RetryFailedEvent) -> None:
        await self._chat_handle_retry_failed(event)

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

        # MID-INJ: if agent is running, send_prompt() returns False because the
        # message was buffered in _pending_injections for mid-run injection.
        # Show it in the chat with an [INJECTING] badge so the user has feedback.
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
            # Buffer the message in the bridge for mid-run system-reminder injection
            self._bridge.send_prompt(raw_val)
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
        self._chat_handle_text_changed(event)

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
            if compacted:
                msg = "Context compacted — context window freed."
                # GAP-MSG-1: visual compaction divider in the message stream.
                divider = Static(
                    "[dim]══════════════ Compaction ══════════════[/]",
                    classes="system_msg compaction_divider",
                    markup=True,
                )
                await self._mount_chat_widget(divider)
                w = Static(f"[dim]{msg}[/]", classes="system_msg", markup=True)
            else:
                # BUG-VOL22-1: Show a warning when compaction fails or has nothing to do.
                msg = "Context compaction failed or nothing to compact — see logs for details."
                self.notify(msg, severity="warning")
                w = Static(f"[bold yellow]{msg}[/]", classes="system_msg", markup=True)
            await self._mount_chat_widget(w)

        elif cmd == "continue":
            if not self._last_task_text:
                self.notify("No previous task to continue", severity="warning")
            else:
                self._bridge.restore_and_continue(
                    self._last_task_text, self._continue_state
                )

        elif cmd == "undo":
            # GAP-CMD-1: Remove the last user message from conversation history.
            if self.agent_running:
                self.notify(
                    "Cannot undo while agent is running — use /interrupt first",
                    severity="warning",
                )
            else:
                await self._slash_undo_with_confirm()

        elif cmd == "revert":
            # P2-2: open SessionScreen so the user can pick a git snapshot to revert to.
            from .screens.session_screen import SessionScreen

            self.push_screen(SessionScreen())

        elif cmd == "interrupt":
            self._bridge.force_interrupt()
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
            self.action_open_sessions()

        elif cmd == "timeline":
            from .screens.timeline import TimelineScreen

            self.push_screen(TimelineScreen(self._bridge.history))

        elif cmd == "diff":
            await self._slash_diff()

        elif cmd == "fork":
            await self._slash_fork()

        elif cmd == "share":
            await self._slash_share()

        elif cmd == "rename":
            await self._slash_rename(args)

        elif cmd == "worktree":
            await self._slash_worktree(args)

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

    async def _slash_undo_with_confirm(self) -> None:
        """P2-2: Show a confirmation dialog before removing the last user message.

        Passes through to the existing undo logic only after the user confirms.
        """
        from textual.containers import Horizontal
        from textual.screen import ModalScreen
        from textual.widgets import Button

        class _UndoConfirmScreen(ModalScreen):
            DEFAULT_CSS = """
            _UndoConfirmScreen { align: center middle; }
            #_undo_dlg {
                background: $surface; border: tall $primary;
                padding: 1 2; width: 56; height: auto;
            }
            #_undo_msg { margin-bottom: 1; }
            #_undo_btns { align: right middle; height: auto; }
            Button { margin-left: 1; }
            """

            def compose(self):
                from textual.containers import Container
                from textual.widgets import Label
                with Container(id="_undo_dlg"):
                    yield Label(
                        "Remove the last user message from history?\n\n"
                        "  This cannot be undone.",
                        id="_undo_msg",
                    )
                    with Horizontal(id="_undo_btns"):
                        yield Button("Undo", id="btn_yes", variant="error")
                        yield Button("Cancel", id="btn_no", variant="default")

            def on_button_pressed(self, event: Button.Pressed) -> None:
                self.dismiss(event.button.id == "btn_yes")

            def on_key(self, event) -> None:
                if event.key == "escape":
                    self.dismiss(False)
                    event.prevent_default()

        async def _after_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                return
            removed = self._bridge.undo_last_user_message()
            if removed:
                with self._bridge._history_lock:
                    hist = self._bridge.history
                    while hist and hist[-1][0] != "user":
                        hist.pop()
                self._bridge._save_history()
                w = Static(
                    "[dim]↩ Undone: last user message removed[/]",
                    classes="system_msg",
                    markup=True,
                )
                await self._mount_chat_widget(w)
                self.notify("Last message undone")
            else:
                self.notify("Nothing to undo", severity="warning")

        self.push_screen(_UndoConfirmScreen(), _after_confirm)

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
                for p in providers:
                    if SettingsStore._provider_matches(p, args):
                        target = p
                        break
            if target is None:
                self.notify(f"Provider not found: {args}", severity="warning")
                return
            pid = SettingsStore._normalize_provider_id(target)
            role = self.active_role
            self._settings.set(f"{role}_provider", pid)
            self._settings.set("default_provider", pid)
            self._settings.save()
            model_list = target.get("models") or [""]
            first_model = model_list[0] if model_list else ""
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
            self.notify(f"Switched to provider: {pid}")

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
                    target_provider = all_models[idx].get("provider_id")
            else:
                q = args.lower()
                for m in all_models:
                    if q == m["model"].lower() or q in m["model"].lower():
                        target_model = m["model"]
                        target_provider = m.get("provider_id")
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
            _cl = _load_config_loader_module()
            get_mcp_servers = _cl.get_mcp_servers
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
                # Prefer a canonical atomic JSON writer when available (lazy import)
                try:
                    from src.core.io_utils import atomic_write_json

                    logger.debug(
                        "app: attempting atomic_write_json for %s", config_path
                    )
                    ok = atomic_write_json(config_path, cfg, logger=logger)
                    if ok:
                        logger.info("MCP config saved atomically: %s", config_path)
                    else:
                        logger.warning(
                            "app: atomic_write_json returned False for %s; falling back",
                            config_path,
                        )
                        raise RuntimeError("atomic_write_json returned False")
                except Exception:
                    # Fallback: write via unique-temp + atomic replace to avoid
                    # exposing partially-written config files.
                    import tempfile as _tempfile
                    import os as _os
                    import shutil as _shutil
                    import traceback as _traceback

                    fd = None
                    tmp = None
                    try:
                        config_path.parent.mkdir(parents=True, exist_ok=True)
                        fd, tmp = _tempfile.mkstemp(
                            dir=str(config_path.parent), suffix=".tmp"
                        )
                        with _os.fdopen(fd, "w", encoding="utf-8") as f:
                            fd = None
                            f.write(_json.dumps(cfg, indent=2))
                            try:
                                f.flush()
                                _os.fsync(f.fileno())
                            except Exception:
                                pass
                        try:
                            _os.replace(tmp, str(config_path))
                        except Exception:
                            try:
                                _shutil.move(tmp, str(config_path))
                            except Exception:
                                logger.debug(
                                    "app: fallback write failed for %s\n%s",
                                    config_path,
                                    _traceback.format_exc(),
                                )
                    finally:
                        try:
                            if fd is not None:
                                _os.close(fd)
                        except Exception:
                            pass
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
                chip_text = str(chip.render()) if chip else "(none)"
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

    async def _slash_share(self) -> None:
        """GAP-CMD-2: export conversation to markdown; copy to clipboard if pyperclip available."""
        import datetime as _dt

        history = list(self._bridge.history)
        lines: list[str] = [
            "# Conversation Export",
            f"_Exported: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
            "",
        ]
        for msg in history:
            # bridge.history is list[tuple[str, str]] — (role, text)
            if isinstance(msg, (list, tuple)) and len(msg) == 2:
                role, content = str(msg[0]), str(msg[1])
            elif isinstance(msg, dict):
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        (p.get("text") or p.get("content") or "")
                        if isinstance(p, dict)
                        else str(p)
                        for p in content  # type: ignore[union-attr]
                    )
            else:
                role, content = "unknown", str(msg)
            lines.append(f"**{role.upper()}**\n\n{content}\n\n---\n")

        md_text = "\n".join(lines)

        # Try clipboard first, then file fallback
        copied = False
        try:
            import pyperclip  # type: ignore[import]

            pyperclip.copy(md_text)
            copied = True
        except Exception:
            pass

        if copied:
            w = Static(
                "[bold #22c55e]✓ Conversation copied to clipboard[/]"
                f"  ({len(history)} messages)",
                classes="system_msg",
                markup=True,
            )
        else:
            from ._core_paths_loader import get_data_dir as _get_data_dir_helper

            export_path = (
                _get_data_dir_helper()
                / f"export_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            )
            try:
                export_path.parent.mkdir(parents=True, exist_ok=True)
                # Write markdown export atomically using mkstemp + os.replace so
                # the user never sees partial export files.
                try:
                    import tempfile as _tempfile
                    import os as _os
                    import shutil as _shutil
                    import traceback as _traceback

                    fd = None
                    tmp = None
                    try:
                        fd, tmp = _tempfile.mkstemp(
                            dir=str(export_path.parent), suffix=".md.tmp"
                        )
                        with _os.fdopen(fd, "w", encoding="utf-8") as f:
                            fd = None
                            f.write(md_text)
                            try:
                                f.flush()
                                _os.fsync(f.fileno())
                            except Exception as _fsync_err:
                                logger.debug("export fsync failed: %s", _fsync_err)
                        try:
                            _os.replace(tmp, str(export_path))
                        except Exception as _replace_err:
                            logger.debug("export replace failed, trying move: %s", _replace_err)
                            try:
                                _shutil.move(tmp, str(export_path))
                            except Exception:
                                logger.debug(
                                    "app: export fallback write failed for %s\n%s",
                                    export_path,
                                    _traceback.format_exc(),
                                )
                    finally:
                        try:
                            if fd is not None:
                                _os.close(fd)
                        except Exception:
                            pass
                except Exception as exc:
                    w = Static(
                        f"[bold #ff5555]✗ Export failed:[/] {exc}",
                        classes="system_msg",
                        markup=True,
                    )
                w = Static(
                    f"[bold]✓ Exported[/] {len(history)} messages\n  → {export_path}",
                    classes="system_msg",
                    markup=True,
                )
            except Exception as exc:
                w = Static(
                    f"[bold #ff5555]✗ Export failed:[/] {exc}",
                    classes="system_msg",
                    markup=True,
                )
        await self._mount_chat_widget(w)

    async def _slash_rename(self, args: str) -> None:
        """GAP-CMD-3: rename the current session."""
        new_name = args.strip()
        if not new_name:
            w = Static(
                "Usage: [bold]/rename <new-name>[/]",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            return

        renamed = False
        try:
            orch = self._bridge._orchestrator  # type: ignore[attr-defined]
            store = getattr(orch, "session_store", None) if orch else None
            current_id: str = getattr(orch, "_current_task_id", "") or ""
            if store is not None and current_id:
                # SessionStore.rename_session if available; otherwise patch metadata
                if hasattr(store, "rename_session"):
                    store.rename_session(current_id, new_name)
                    renamed = True
                elif hasattr(store, "_sessions") and current_id in store._sessions:
                    store._sessions[current_id]["title"] = new_name
                    renamed = True
        except Exception as exc:
            logger.warning(f"/rename: session rename failed: {exc}")

        # Update app sub_title reactive (same as line 667 for role transitions)
        try:
            self.sub_title = new_name
        except Exception:
            pass
        self._bridge.publish("session.renamed", {"name": new_name})

        msg = (
            f"[bold]Session renamed:[/] {new_name}"
            if renamed
            else f"[bold]Display name set:[/] {new_name} [dim](session store not updated)[/]"
        )
        w = Static(msg, classes="system_msg", markup=True)
        await self._mount_chat_widget(w)

    async def _slash_worktree(self, args: str) -> None:
        """GAP-WORKTREE-1: manage git worktree isolation for tasks."""
        from pathlib import Path as _Path

        try:
            from src.core.orchestration.git_worktree_manager import GitWorktreeManager  # type: ignore[import]
        except ImportError:
            self.notify("GitWorktreeManager not available", severity="error")
            return

        orch = self._bridge._orchestrator  # type: ignore[attr-defined]
        workspace = _Path(getattr(orch, "working_dir", ".") if orch else ".")
        if not hasattr(self, "_worktree_mgr"):
            self._worktree_mgr = GitWorktreeManager(workspace)  # type: ignore[attr-defined]

        parts = args.strip().split(None, 1)
        sub = parts[0].lower() if parts else "list"
        sub_args = parts[1] if len(parts) > 1 else ""

        if sub == "list":
            registered = await self._worktree_mgr.list_registered()
            active = self._worktree_mgr.active
            if not registered:
                w = Static("No worktrees registered", classes="system_msg")
            else:
                lines = []
                for entry in registered:
                    path = entry.get("worktree", "?")
                    head = entry.get("HEAD", "?")[:8]
                    branch = entry.get("branch") or (
                        "detached" if entry.get("detached") else "?"
                    )
                    marker = (
                        " [agent]" if path in (str(v) for v in active.values()) else ""
                    )
                    lines.append(f"  {path}  {head}  [{branch}]{marker}")
                w = Static(
                    "[bold]Worktrees:[/]\n" + "\n".join(lines),
                    classes="system_msg",
                    markup=True,
                )
            await self._mount_chat_widget(w)

        elif sub == "create":
            task_id = sub_args.strip() or (
                getattr(orch, "_current_task_id", None) or "manual"
                if orch
                else "manual"
            )
            try:
                wt_path = await self._worktree_mgr.create(task_id)
                w = Static(
                    f"[bold #22c55e]✓ Worktree created[/]\n"
                    f"  task_id: {task_id}\n"
                    f"  path:    {wt_path}",
                    classes="system_msg",
                    markup=True,
                )
            except Exception as exc:
                w = Static(
                    f"[bold #ff5555]✗ Worktree create failed:[/] {exc}",
                    classes="system_msg",
                    markup=True,
                )
            await self._mount_chat_widget(w)

        elif sub == "remove":
            task_id = sub_args.strip()
            if not task_id:
                self.notify("Usage: /worktree remove <task_id>", severity="warning")
                return
            removed = await self._worktree_mgr.remove(task_id)
            msg = (
                f"[bold #22c55e]✓ Worktree removed:[/] {task_id}"
                if removed
                else f"[dim]No worktree found for task_id: {task_id}[/]"
            )
            await self._mount_chat_widget(
                Static(msg, classes="system_msg", markup=True)
            )

        else:
            w = Static(
                "Usage: [bold]/worktree[/] [list | create [<task_id>] | remove <task_id>]",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)

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

    def _activate_provider(self, provider_id: str) -> bool:
        """Switch the active provider at runtime and persist the choice.

        1. Writes providers.json atomically — sets the named provider
           active:true and all others active:false in a single write so the
           selection survives a restart.
        2. Fetches the adapter from ProviderManager and assigns it to the
           Orchestrator (``orchestrator.adapter = …``), which also fires
           ``_publish_active_config`` so the banner and sidebar update.
        3. Re-publishes provider status via the bridge so the TUI reflects the
           change immediately even if no bus event arrives.

        Returns True if the adapter was successfully swapped, False otherwise.
        """
        try:
            _lm = _load_llm_manager_module()
            get_provider_manager = _lm.get_provider_manager
            canonical_provider = _lm.canonical_provider
            resolve_config_path = _lm.resolve_config_path
            _providers_json_lock = _lm._providers_json_lock

            norm_id = canonical_provider(provider_id)
            pm = get_provider_manager()

            # Step 1: single atomic write — flip exactly one entry to active:true.
            import json as _json
            import os as _os
            import tempfile as _tf

            cfg_path = resolve_config_path(None)
            with _providers_json_lock:
                raw = _json.loads(cfg_path.read_text(encoding="utf-8"))
                entries = raw if isinstance(raw, list) else [raw]
                matched = False
                for p in entries:
                    key = canonical_provider(p.get("type") or p.get("name") or "")
                    p["active"] = key == norm_id
                    if key == norm_id:
                        matched = True
                new_text = _json.dumps(entries, indent=2)
                fd, tmp = _tf.mkstemp(dir=cfg_path.parent, suffix=".tmp")
                try:
                    with _os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(new_text)
                    _os.replace(tmp, cfg_path)
                except Exception:
                    try:
                        _os.unlink(tmp)
                    except OSError:
                        pass
                    raise

            if not matched:
                logger.warning(
                    f"_activate_provider: no providers.json entry matched '{norm_id}'"
                )

            logger.info(
                f"_activate_provider: persisted active={norm_id} to providers.json"
            )

            # Step 2: get adapter and assign to orchestrator
            adapter = pm.get_provider(norm_id)
            orch = getattr(self._bridge, "_orchestrator", None)
            if orch is not None:
                orch.adapter = adapter  # setter also calls _publish_active_config
                logger.info(
                    f"_activate_provider: swapped orchestrator adapter to {norm_id} ({adapter})"
                )
            else:
                logger.warning("_activate_provider: no orchestrator found on bridge")

            # Step 3: re-publish status to update TUI banner/sidebar
            try:
                self._bridge._publish_active_provider_status()
            except Exception as _pub_exc:
                logger.debug(f"_activate_provider re-publish: {_pub_exc}")

            return True
        except Exception as exc:
            logger.error(f"_activate_provider({provider_id}): {exc}")
            return False

    @on(ConnectProvider)
    def handle_connect_provider(self, event: ConnectProvider) -> None:
        """Route to the appropriate connection screen for the provider.

        GitHub Copilot uses the OAuth device flow (DeploymentSelectScreen →
        OAuthDeviceFlowScreen), matching OpenCode's dialog-connect-provider flow.

        For other providers:
        - If the provider already has an adapter registered in ProviderManager
          (i.e. it was configured previously), activate it immediately so the
          Orchestrator starts using it right away, then open the config screen
          only to allow updating credentials.
        - If there is no adapter yet, open the config screen so the user can
          supply the API key; activation happens in handle_save_credentials.
        """
        _COPILOT_IDS = {"github_copilot", "githubcopilot", "github-copilot"}
        if event.provider_id.lower().replace("-", "_") in _COPILOT_IDS:
            from .features.oauth.screen import DeploymentSelectScreen

            self.push_screen(DeploymentSelectScreen())
            return

        # Try to activate immediately if the provider is already configured.
        try:
            _lm = _load_llm_manager_module()
            get_provider_manager = _lm.get_provider_manager
            canonical_provider = _lm.canonical_provider

            pm = get_provider_manager()
            adapter = pm.get_provider(canonical_provider(event.provider_id))
            if adapter is not None:
                logger.info(
                    f"handle_connect_provider: activating already-configured provider {event.provider_id}"
                )
                self._activate_provider(event.provider_id)
                self.notify(
                    f"Switched to {event.provider_id.replace('_', ' ').title()}",
                    severity="information",
                )
                return
        except Exception as _chk_exc:
            logger.debug(f"handle_connect_provider provider check: {_chk_exc}")

        from .features.settings.screen import ProviderConfigScreen

        prov = self._settings.get_provider_by_id(event.provider_id)
        name = prov["name"] if prov else event.provider_id.replace("_", " ").title()
        existing_base_url = prov.get("base_url", "") if prov else ""
        self.push_screen(
            ProviderConfigScreen(
                event.provider_id, name, existing_base_url=existing_base_url
            )
        )

    @on(SaveProviderCredentials)
    def handle_save_credentials(self, event: SaveProviderCredentials) -> None:
        logger.info(f"Save provider credentials: {event.provider_id}")
        try:
            from tui.tui_src.ui.config_writer import save_provider_credentials

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
            return

        # After saving credentials, activate the provider so the Orchestrator
        # immediately switches to it (covers the "first time setup" path where
        # handle_connect_provider found no existing adapter).
        self._activate_provider(event.provider_id)

    @on(UpdateRoleModel)
    def handle_update_role_model(self, event: UpdateRoleModel) -> None:
        logger.info(
            f"Role model update: {event.role} → {event.model_id} (provider={event.provider_id})"
        )

        # If the model belongs to a different provider than the one currently
        # active, switch provider first.  This ensures the orchestrator adapter
        # and the TUI banner both reflect the correct provider name.
        if event.provider_id:
            try:
                _lm = _load_llm_manager_module()
                canonical_provider = _lm.canonical_provider

                norm_req = canonical_provider(event.provider_id)
                orch = getattr(self._bridge, "_orchestrator", None)
                current_adapter = getattr(orch, "_adapter", None) if orch else None

                # Determine the canonical key of the currently active adapter.
                current_provider_key = None
                if current_adapter is not None:
                    prov_dict = getattr(current_adapter, "provider", None)
                    if isinstance(prov_dict, dict):
                        raw = prov_dict.get("name") or prov_dict.get("type") or ""
                        current_provider_key = canonical_provider(raw)
                    if not current_provider_key:
                        current_provider_key = canonical_provider(
                            getattr(current_adapter, "name", "") or ""
                        )

                if current_provider_key != norm_req:
                    logger.info(
                        f"handle_update_role_model: provider mismatch "
                        f"({current_provider_key} → {norm_req}), switching provider"
                    )
                    self._activate_provider(event.provider_id)
                    # Re-fetch adapter after switch
                    orch = getattr(self._bridge, "_orchestrator", None)
                    current_adapter = getattr(orch, "_adapter", None) if orch else None
            except Exception as _exc:
                logger.debug(f"handle_update_role_model provider switch: {_exc}")

        # Apply the model selection to the (now-correct) active adapter.
        # Sanitize user-supplied model id to avoid leaking test doubles like
        # MagicMock placeholders into the adapter state. Preserve list identity
        # when updating adapter.models so other components holding a reference
        # to the list continue to observe changes.
        try:
            orch = getattr(self._bridge, "_orchestrator", None)
            if orch is not None:
                adapter = getattr(orch, "_adapter", None)
                if adapter is not None:
                    # Local sanitiser: accept concrete non-empty strings and
                    # reject MagicMock placeholders. Keep this local so the
                    # TUI remains self-contained in dev mode.
                    def _valid_str(x: object) -> bool:
                        return (
                            isinstance(x, str)
                            and bool(x.strip())
                            and ("MagicMock" not in x)
                        )

                    # Extract a model id string from the event payload.
                    model_id = None
                    try:
                        if isinstance(event.model_id, str) and _valid_str(
                            event.model_id
                        ):
                            model_id = event.model_id.strip()
                    except Exception:
                        model_id = None

                    if not model_id:
                        logger.debug(
                            f"handle_update_role_model: ignoring invalid model id: {event.model_id}"
                        )
                    else:
                        # Try common attribute names for the model setting.
                        for _attr in ("default_model", "model", "_model"):
                            if hasattr(adapter, _attr):
                                try:
                                    setattr(adapter, _attr, model_id)
                                    logger.info(
                                        f"handle_update_role_model: set adapter.{_attr} = {model_id}"
                                    )
                                except Exception:
                                    logger.debug(
                                        f"handle_update_role_model: failed to set adapter.{_attr}"
                                    )
                                break

                        # Keep models list consistent so _publish_active_config picks
                        # the right model as models[0]. Update in-place to preserve
                        # list identity and filter out any MagicMock placeholders.
                        if hasattr(adapter, "models") and isinstance(
                            adapter.models, list
                        ):
                            try:
                                # Remove invalid entries first
                                valid_models = [
                                    m for m in adapter.models if _valid_str(m)
                                ]
                                adapter.models[:] = valid_models
                                if model_id in adapter.models:
                                    adapter.models.remove(model_id)
                                adapter.models.insert(0, model_id)
                            except Exception:
                                # Best-effort in-place update failed; try simple insert
                                try:
                                    if model_id not in adapter.models:
                                        adapter.models.insert(0, model_id)
                                except Exception as _model_ins_err:
                                    logger.debug("model insert best-effort failed: %s", _model_ins_err)

                        # Re-publish so banner/sidebar refresh immediately.
                        if hasattr(orch, "_publish_active_config"):
                            try:
                                orch._publish_active_config()
                            except Exception as _pub_err:
                                logger.debug("publish active config failed: %s", _pub_err)
        except Exception as _exc:
            logger.debug(f"handle_update_role_model adapter update: {_exc}")

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

    # ── GitHub Copilot OAuth device flow ──────────────────────────────────────

    @on(StartGithubDeviceFlow)
    async def handle_start_github_device_flow(
        self, event: StartGithubDeviceFlow
    ) -> None:
        """User confirmed deployment type in DeploymentSelectScreen.

        Calls start_device_flow() (a fast POST to GitHub or GHE), then opens
        the OAuthDeviceFlowScreen modal and launches a background thread that
        runs poll_for_token().  The thread posts DeviceFlowCompleteEvent or
        DeviceFlowErrorEvent when finished; those events close the modal and
        notify the user.
        """
        import threading as _threading

        try:
            _auth = _load_copilot_auth_module()
            start_device_flow = _auth.start_device_flow
            poll_for_token = _auth.poll_for_token
            save_token = _auth.save_token
            DeviceCodeExpired = _auth.DeviceCodeExpired
            AuthCancelled = _auth.AuthCancelled
        except ImportError as exc:
            logger.error(f"Cannot import github_copilot_auth: {exc}")
            self.notify("GitHub Copilot auth module not available.", severity="error")
            return

        enterprise_url = getattr(event, "enterprise_url", None)

        # Kick off the device code request (fast — one HTTP POST)
        try:
            flow = start_device_flow(enterprise_url=enterprise_url)
        except Exception as exc:
            logger.error(f"start_device_flow failed: {exc}")
            self.notify(f"Failed to start GitHub login: {exc}", severity="error")
            return

        # Reset any previous cancellation signal
        self._device_flow_cancel.clear()

        # Open the modal — user sees the code and link immediately
        from .features.oauth.screen import OAuthDeviceFlowScreen

        oauth_screen = OAuthDeviceFlowScreen(
            user_code=flow.user_code,
            verification_uri=flow.verification_uri,
            expires_in=flow.expires_in,
        )
        self._oauth_screen = oauth_screen
        await self.push_screen(oauth_screen)

        # Launch background polling thread — does NOT block the TUI event loop
        cancel_evt = self._device_flow_cancel
        domain = flow.domain

        def _poll_thread() -> None:
            try:
                token = poll_for_token(
                    flow.device_code,
                    flow.interval,
                    domain=domain,
                    timeout=flow.expires_in,
                    cancel_event=cancel_evt,
                )
                save_token(token, enterprise_url=enterprise_url)
                # Post success back to the Textual event loop
                self.call_from_thread(
                    self.post_message,
                    DeviceFlowCompleteEvent(provider_id="github_copilot"),
                )
            except DeviceCodeExpired:
                self.call_from_thread(
                    self.post_message,
                    DeviceFlowErrorEvent(
                        reason="Device code expired — please try again."
                    ),
                )
            except AuthCancelled:
                self.call_from_thread(
                    self.post_message,
                    DeviceFlowErrorEvent(reason="Authorization was denied on GitHub."),
                )
            except Exception as exc:
                if cancel_evt.is_set():
                    return  # user cancelled — no error toast
                self.call_from_thread(
                    self.post_message,
                    DeviceFlowErrorEvent(reason=str(exc)),
                )

        _threading.Thread(target=_poll_thread, daemon=True, name="copilot-poll").start()

    @on(DeviceFlowCompleteEvent)
    def handle_device_flow_complete(self, event: DeviceFlowCompleteEvent) -> None:
        """Token received and saved — close the modal, update the banner, and notify."""
        # Dismiss the OAuthDeviceFlowScreen modal if it is still open
        if self._oauth_screen is not None:
            try:
                screen = self._oauth_screen
                self._oauth_screen = None
                screen.complete()  # type: ignore[attr-defined]
            except Exception as _oauth_err:
                logger.debug("dismiss OAuth screen failed: %s", _oauth_err)

        # Also dismiss any open SettingsScreen — it was constructed before the
        # token existed so it still shows the "Login" button.  Dismissing it
        # causes the user to reopen settings fresh and see "Connected" instead.
        try:
            from .features.settings.screen import SettingsScreen

            for screen in list(self.screen_stack):
                if isinstance(screen, SettingsScreen):
                    screen.dismiss()
                    break
        except Exception as _settings_err:
            pass

        # Immediately update the provider banner / sidebar — no restart needed.
        provider_id = (
            getattr(event, "provider_id", "github_copilot") or "github_copilot"
        )
        # Look up the display name from settings (falls back to a title-cased id)
        prov = self._settings.get_provider_by_id(provider_id)
        provider_name = prov["name"] if prov else provider_id.replace("_", " ").title()
        try:
            from .bus import ProviderStatusChangeEvent as _PSCE

            self.post_message(
                _PSCE(
                    provider=provider_name,
                    new_status="connected",
                    old_status="disconnected",
                )
            )
        except Exception:
            pass

        self.notify(
            f"{provider_name} connected! Reopen Settings to see updated status.",
            severity="information",
            timeout=6,
        )
        logger.info("GitHub Copilot OAuth: token saved successfully")

    @on(DeviceFlowErrorEvent)
    def handle_device_flow_error(self, event: DeviceFlowErrorEvent) -> None:
        """Flow failed or was cancelled — close the modal and report."""
        # Signal the polling thread to stop (if reason == "cancelled")
        self._device_flow_cancel.set()

        if self._oauth_screen is not None:
            try:
                screen = self._oauth_screen
                self._oauth_screen = None
                screen.fail(event.reason)  # type: ignore[attr-defined]
            except Exception:
                pass

        if event.reason and event.reason != "cancelled":
            self.notify(
                f"GitHub Copilot login failed: {event.reason}",
                severity="error",
                timeout=8,
            )

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
            from .components import ConsolePanel

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
        try:
            from .features.settings.screen import SettingsScreen

            self.push_screen(SettingsScreen(self._settings))
        except Exception as exc:
            # Preserve stacktrace at ERROR level for diagnostics
            logger.exception(f"action_open_settings failed: {exc}")
            self.notify(f"Could not open settings: {exc}", severity="error")

    def action_open_sessions(self) -> None:
        """P3-2: Open the session browser (Ctrl+R or /sessions command)."""
        from .screens.session_screen import SessionScreen

        self.push_screen(SessionScreen())

    # Legacy /clear helper alias
    def _clear_session(self) -> None:
        self._clear_chat_panel()
        self._reset_sidebar()
        self.notify("Session cleared")
