"""Textual-based TUI implementation for CodingAgent.

This module implements a minimal Textual application with:
- Sidebar showing provider and model
- Main chat output area
- Input box to send prompts to the Orchestrator

The module lazily imports Textual so importing the module in environments with
Textual installed does not crash; instead `TextualAppStub` is provided which
prints guidance when run.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional, Dict, Any, TYPE_CHECKING
import importlib

from src.core.orchestration.event_bus import get_event_bus
from src.core.inference.llm_manager import get_provider_manager
from src.core.orchestration.orchestrator import Orchestrator
from src.core.logger import logger as guilogger
from src.ui.views.settings_panel import SettingsPanelController

if TYPE_CHECKING:
    # These imports are only for static type checkers (IDE/linter). At runtime we
    # dynamically import textual via importlib so the module can be imported on
    # systems without Textual installed.
    from textual.app import App, ComposeResult  # type: ignore
    from textual.containers import Container, Horizontal  # type: ignore
    from textual.widgets import Header, Footer, Static, Input, RichLog, Button  # type: ignore
    from textual.events import Key, Paste  # type: ignore
    from textual.screen import ModalScreen  # type: ignore
    from textual.widgets import Select, Label  # type: ignore

TEXTUAL_AVAILABLE: bool = False

# Runtime fallbacks — only evaluated when NOT type-checking so they don't shadow
# the TYPE_CHECKING imports above.
if not TYPE_CHECKING:
    App: Any = object
    Container: Any = None
    Horizontal: Any = None
    Header: Any = None
    Footer: Any = None
    Static: Any = None
    Input: Any = None
    RichLog: Any = None
    Button: Any = None
    ComposeResult: Any = None
    Select: Any = None
    Label: Any = None
    ModalScreen: Any = object
    try:
        _textual_app = importlib.import_module("textual.app")
        App = getattr(_textual_app, "App")
        ComposeResult = getattr(_textual_app, "ComposeResult")
        _containers = importlib.import_module("textual.containers")
        Container = getattr(_containers, "Container")
        Horizontal = getattr(_containers, "Horizontal")
        _widgets = importlib.import_module("textual.widgets")
        Header = getattr(_widgets, "Header")
        Footer = getattr(_widgets, "Footer")
        Static = getattr(_widgets, "Static")
        Input = getattr(_widgets, "Input")
        RichLog = getattr(_widgets, "RichLog")
        Button = getattr(_widgets, "Button")
        Select = getattr(_widgets, "Select")
        Label = getattr(_widgets, "Label")
        _screen = importlib.import_module("textual.screen")
        ModalScreen = getattr(_screen, "ModalScreen")
        TEXTUAL_AVAILABLE = True
    except Exception:
        # Textual isn't available at runtime; fall back to plain-mode.
        pass


class TextualAppBase:
    """Base behavior used by both real Textual app and stub."""

    def __init__(self, orchestrator: Optional[Orchestrator] = None):
        self.orchestrator = orchestrator or Orchestrator()
        # internal chat history as tuple(role, text)
        self.history: List[tuple] = []
        self._history_lock = threading.Lock()  # H4: protects concurrent history access
        # P3-10: Load persisted history from disk on startup
        self._load_history()
        # H3 fix: mutex that prevents two send_prompt() calls from running simultaneously.
        # Without this, concurrent calls race on _session_read_files, msg_mgr, and
        # _step_snapshot_id on the shared orchestrator.
        self._agent_lock = threading.Lock()
        self._agent_running = False
        # event bus
        try:
            self.event_bus = get_event_bus()
            if self.event_bus:
                self.event_bus.subscribe("ui.notification", self._on_ui_notification)
        except Exception:
            self.event_bus = None
        # cancel event for interrupting agent
        self._cancel_event = threading.Event()

    # P3-10: Conversation history persistence
    def _get_history_path(self) -> "Path":
        from pathlib import Path
        import os

        hist_dir = Path(os.path.expanduser("~/.coding_agent"))
        hist_dir.mkdir(parents=True, exist_ok=True)
        return hist_dir / "tui_conversation_history.json"

    def _load_history(self) -> None:
        """Load persisted conversation history from disk into self.history."""
        import json

        try:
            p = self._get_history_path()
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.history = [
                        tuple(entry)
                        for entry in data
                        if isinstance(entry, (list, tuple)) and len(entry) == 2
                    ]
        except Exception:
            pass  # never block startup on history load failure

    def _save_history(self) -> None:
        """Persist current conversation history to disk atomically."""
        import json, tempfile, os

        try:
            p = self._get_history_path()
            with self._history_lock:
                snapshot = list(self.history)
            serialisable = [list(entry) for entry in snapshot]
            tmp_fd, tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as _f:
                    json.dump(serialisable, _f, ensure_ascii=False)
                os.replace(tmp_path, p)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            pass  # never block agent on history save failure

    @property
    def is_agent_running(self) -> bool:
        """Return True if an agent task is currently running."""
        with self._agent_lock:
            return self._agent_running

    def send_prompt(self, prompt: str) -> None:
        """Send a prompt to the orchestrator in a background thread.

        H3 fix: rejects new prompts while the agent is already running to prevent
        concurrent access to shared orchestrator state (race conditions on
        _session_read_files, msg_mgr, _step_snapshot_id, etc.).
        """
        with self._agent_lock:
            if self._agent_running:
                guilogger.warning(
                    "send_prompt: agent already running, ignoring duplicate submission"
                )
                return
            self._agent_running = True

        with self._history_lock:
            self.history.append(("user", prompt))
        self._save_history()
        self._agent_thread = threading.Thread(
            target=self._run_agent, args=(prompt,), daemon=True
        )
        self._agent_thread.start()

    def _run_agent(self, prompt: str) -> None:
        try:
            # Clear execution trace and start new task with fresh state
            if hasattr(self.orchestrator, "_clear_execution_trace"):
                self.orchestrator._clear_execution_trace()
            # Start new task (generates ID and clears message history)
            if hasattr(self.orchestrator, "start_new_task"):
                task_id = self.orchestrator.start_new_task()
                guilogger.info(f"Starting task {task_id}: {prompt[:50]}...")
            # Clear cancel event for new task
            self._cancel_event.clear()
            # Build messages list (system prompt is injected by orchestrator)
            messages = [{"role": "user", "content": prompt}]
            res = self.orchestrator.run_agent_once(
                None, messages, {}, cancel_event=self._cancel_event
            )
            # res expected to have 'assistant_message' or 'raw'
            assistant_msg = None
            work_summary = None
            try:
                if isinstance(res, dict) and res.get("assistant_message"):
                    assistant_msg = res.get("assistant_message")
                    work_summary = res.get("work_summary")
                elif isinstance(res, dict) and res.get("raw"):
                    raw = res.get("raw")
                    if isinstance(raw, dict):
                        # attempt to extract typical openai-like response
                        ch = raw.get("choices")
                        if ch and isinstance(ch, list) and ch[0].get("message"):
                            assistant_msg = ch[0]["message"].get("content")
                        else:
                            assistant_msg = str(raw)
                    else:
                        assistant_msg = str(raw)
                else:
                    assistant_msg = str(res)
            except Exception:
                assistant_msg = str(res)
            if assistant_msg is None:
                assistant_msg = ""
            if work_summary:
                assistant_msg = assistant_msg + "\n" + work_summary
            # append
            with self._history_lock:
                self.history.append(("assistant", assistant_msg))
            self._save_history()
            # notify UI thread by calling `on_agent_result` if present
            try:
                # schedule callback safely on the main loop
                self._schedule_callback(self.on_agent_result, assistant_msg)
            except Exception:
                try:
                    self.on_agent_result(assistant_msg)
                except Exception:
                    pass
        except Exception as e:
            guilogger.error(f"TextualApp: _run_agent failed: {e}")
            try:
                self.on_agent_result(
                    "[ERROR] Agent encountered an unexpected error. Check logs for details."
                )
            except Exception:
                pass
        finally:
            # H3 fix: always release the running lock so subsequent prompts are accepted.
            with self._agent_lock:
                self._agent_running = False

    def on_agent_result(self, content: str) -> None:
        """Hook executed in the UI thread when agent result is ready.
        UI implementations should override this method to update widgets."""
        # default: log
        guilogger.info(f"Agent result: {content}")

    def _on_ui_notification(self, payload) -> None:
        """Handle ui.notification events from the event bus."""
        if isinstance(payload, dict):
            level = payload.get("level", "info")
            message = payload.get("message", "")
            if message:
                level_indicator = {
                    "warning": "[yellow]⚠[/yellow] ",
                    "error": "[red]✖[/red] ",
                    "info": "[blue]ℹ[/blue] ",
                }.get(level, "")
                self._safe_write(f"{level_indicator}{message}")

    def _safe_write(self, msg: str) -> None:
        """Write to the output using rich markup if available, otherwise plain text.

        This centralizes the logic to handle environments without `rich` or where
        the `RichLog` widget isn't present. It silently falls back to plain
        writes when needed.
        """
        try:
            out = getattr(self, "output", None)
            if not out:
                return
            # Try rich.text if available
            try:
                from rich.text import Text  # type: ignore

                try:
                    out.write(Text.from_markup(msg))
                    return
                except Exception:
                    # fall through to plain write
                    pass
            except Exception:
                pass
            # Plain write fallback
            try:
                out.write(msg)
            except Exception:
                # last resort: log
                guilogger.info(msg)
        except Exception:
            # never raise from UI write
            pass

    def _schedule_callback(self, fn, *args, **kwargs):
        """Schedule a synchronous callback to run on the main asyncio loop if available.

        This avoids using Textual's call_from_thread which in some environments
        returned a coroutine that wasn't awaited, causing runtime warnings.
        """
        try:
            import asyncio

            loop = None
            try:
                loop = asyncio.get_event_loop()
            except Exception:
                loop = None
            if loop and loop.is_running():
                # schedule safely from background thread
                loop.call_soon_threadsafe(lambda: fn(*args, **kwargs))
                return
        except Exception:
            pass
        # fallback to direct call
        try:
            fn(*args, **kwargs)
        except Exception:
            pass


if not TEXTUAL_AVAILABLE:

    class TextualAppStub(TextualAppBase):
        def run(self) -> None:
            print("Textual is not installed in this environment.")
            print("Install it in your venv with: pip install textual")
            print(
                "Alternatively, run the headless app shim via src.ui.app.CodingAgentApp"
            )

    def create_app(orchestrator: Optional[Orchestrator] = None):  # type: ignore[reportRedeclaration]
        return TextualAppStub(orchestrator=orchestrator)

else:
    # Dynamically import textual.events to avoid static import errors when Textual is absent
    try:
        _events = importlib.import_module("textual.events")
        Key = getattr(_events, "Key")
        Paste = getattr(_events, "Paste")
    except Exception:
        Key = object
        Paste = object

    # Fix 4: Module-level compiled regex patterns — avoid recompiling per message
    import re as _re

    _DIFF_PATTERN = _re.compile(r"```diff\n(.*?)\n```", _re.DOTALL)
    _THINKING_PATTERN = _re.compile(r"<think>(.*?)</think>", _re.DOTALL)
    _HUNK_PATTERN = _re.compile(r"@@ -(\d+),?\d* \+(\d+),?\d* @@")

    # M3: Known slash commands for Tab autocomplete
    SLASH_COMMANDS = [
        "/help",
        "/clear",
        "/compact",
        "/continue",
        "/interrupt",
        "/model",
        "/new",
        "/provider",
        "/quit",
        "/settings",
        "/status",
        "/history",
        "/reset",
        "/workdir",
        "/benchmark",
    ]

    class ChatInput(Input):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.history = []
            self.history_index = -1
            self._tab_matches: list = []
            self._tab_index: int = -1

        def on_key(self, event: Key) -> None:
            if event.key == "tab":
                # M3: Slash command autocomplete
                current = self.value
                if current.startswith("/"):
                    matches = [c for c in SLASH_COMMANDS if c.startswith(current)]
                    if matches:
                        # Cycle through matches on repeated Tab
                        if self._tab_matches != matches:
                            self._tab_matches = matches
                            self._tab_index = 0
                        else:
                            self._tab_index = (self._tab_index + 1) % len(matches)
                        self.value = self._tab_matches[self._tab_index]
                        self.cursor_position = len(self.value)
                    event.prevent_default()
                else:
                    # Reset tab state when not completing slash commands
                    self._tab_matches = []
                    self._tab_index = -1
            else:
                # Any non-Tab key resets completion state
                if event.key not in ("shift+tab",):
                    self._tab_matches = []
                    self._tab_index = -1
            if event.key == "up":
                if self.history and self.history_index < len(self.history) - 1:
                    if self.history_index == -1:
                        # save current draft?
                        pass
                    self.history_index += 1
                    self.value = self.history[
                        len(self.history) - 1 - self.history_index
                    ]
                    self.cursor_position = len(self.value)
                event.prevent_default()
            elif event.key == "down":
                if self.history_index > 0:
                    self.history_index -= 1
                    self.value = self.history[
                        len(self.history) - 1 - self.history_index
                    ]
                    self.cursor_position = len(self.value)
                elif self.history_index == 0:
                    self.history_index = -1
                    self.value = ""
                    self.cursor_position = 0
                event.prevent_default()

        def on_paste(self, event: Paste) -> None:
            # Compact multi-line pastes by replacing newlines with literal "\n"
            # so they fit in the single line input, but the orchestrator can unescape them.
            if event.text:
                compact_text = event.text.replace("\n", "\\n").replace("\r", "")
                # Insert at cursor
                self.insert_text_at_cursor(compact_text)
                event.prevent_default()

    class CodingAgentTextualApp(App, TextualAppBase):
        CSS_PATH = "styles/main.tcss"
        BINDINGS = [
            ("ctrl+o", "open_settings", "Settings"),
            ("ctrl+l", "toggle_log", "Log"),
            ("ctrl+q", "quit_app", "Quit"),
            ("escape", "interrupt_agent", "Interrupt"),
            ("escape,escape", "force_interrupt_agent", "Force Interrupt"),
        ]

        def __init__(self, orchestrator: Optional[Orchestrator] = None):
            App.__init__(self)
            TextualAppBase.__init__(self, orchestrator=orchestrator)
            self.output: Optional[RichLog] = None
            self.sys_log: Optional[RichLog] = None
            self.mode_label: Optional[Static] = None
            self.provider_model_label: Optional[Static] = None
            self._agent_running = False
            self._agent_thread: Optional[threading.Thread] = None
            self._cancel_event = threading.Event()
            self._continue_state: Optional[Dict[str, Any]] = None
            self.context_label: Optional[Static] = None
            self.input_widget: Optional[Input] = None
            # C2: Dashboard labels wired to EventBus plan/tool events
            self.plan_progress_label: Optional[Static] = None
            self.tool_activity_label: Optional[Static] = None
            # H7: Track EventBus subscriptions for cleanup in on_unmount
            self._eb_subscriptions: List[tuple] = []

            self._current_provider = "None"
            self._current_model = "None"

        def _schedule_callback(self, fn, *args, **kwargs):
            """Fix 3: Override base class to use Textual's call_from_thread (Python 3.10+ safe)."""
            try:
                self.call_from_thread(fn, *args, **kwargs)
            except Exception:
                try:
                    fn(*args, **kwargs)
                except Exception:
                    pass

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="app_grid"):
                with Container(id="main_area"):
                    self.output = RichLog(highlight=True, id="chat_output", wrap=True)
                    yield self.output

                    self.sys_log = RichLog(highlight=True, id="sys_log")
                    self.sys_log.display = False
                    yield self.sys_log

                    with Container(id="input_container"):
                        with Horizontal(id="input_status_bar"):
                            self.mode_label = Static("Mode: Auto", id="mode_info")
                            self.provider_model_label = Static(
                                "Provider: None | Model: None", id="provider_model_info"
                            )
                            yield self.mode_label
                            yield self.provider_model_label

                        self.input_widget = ChatInput(
                            placeholder="Type prompt and press Enter...",
                            id="chat_input",
                        )
                        yield self.input_widget

                        with Horizontal(id="input_legend"):
                            yield Static(
                                "⌨️  Tab /cmd | Ctrl+O Settings | Ctrl+L Log (toggle) | Esc Interrupt | Double-Esc Force Stop | Enter Send",
                                id="legend_info",
                            )

                with Container(id="sidebar"):
                    with Container(id="sidebar_top"):
                        yield Static("📊 Context", classes="sidebar_title")
                        # U5: Context limit is read dynamically from provider config
                        try:
                            from src.core.inference.provider_context import (
                                get_context_budget,
                            )

                            ctx_limit = get_context_budget()
                            ctx_limit_str = (
                                f"{ctx_limit // 1000}k"
                                if ctx_limit >= 1000
                                else str(ctx_limit)
                            )
                        except Exception:
                            ctx_limit_str = "?"
                        self.context_label = Static(
                            f"Used: 0\nLimit: {ctx_limit_str}\n0%", id="context_info"
                        )
                        yield self.context_label

                        yield Static("📝 Task State", classes="sidebar_title")
                        self.task_state_label = Static(
                            "No active task", id="task_state_info"
                        )
                        yield self.task_state_label

                        # C2: Plan progress panel — updated by plan.progress EventBus events
                        yield Static("📋 Plan Progress", classes="sidebar_title")
                        self.plan_progress_label = Static(
                            "No active plan", id="plan_progress_info"
                        )
                        yield self.plan_progress_label

                        # C2: Tool activity panel — updated by tool.execute events
                        yield Static("🔧 Last Tool", classes="sidebar_title")
                        self.tool_activity_label = Static("—", id="tool_activity_info")
                        yield self.tool_activity_label

                    with Container(id="sidebar_bottom"):
                        yield Static("📁 Workspace", classes="sidebar_title")
                        import os

                        wd = (
                            getattr(self.orchestrator, "working_dir", None)
                            or os.getcwd()
                        )
                        yield Static(str(wd), id="working_dir_info")
            yield Footer()

        async def on_mount(self) -> None:
            # Wire provider manager event bus to our event bus
            try:
                pm = get_provider_manager()
                if pm:
                    # Sync init to ensure providers are loaded
                    from src.core.inference.llm_manager import (
                        _ensure_provider_manager_initialized_sync,
                    )

                    _ensure_provider_manager_initialized_sync()

                    if getattr(pm, "_event_bus", None) is None:
                        pm.set_event_bus(get_event_bus())
            except Exception:
                pass

            # subscribe to events — H7: track all subscriptions for cleanup in on_unmount
            try:
                eb = get_event_bus()
                if eb:
                    _subs = [
                        ("provider.status.changed", self._on_provider_status_changed),
                        ("provider.models.list", self._on_provider_models),
                        ("log.new", self._on_log_new),
                        ("model.response", self._on_token_usage),
                        ("model.routing", self._on_model_routing),
                        ("session.new", self._on_session_new),
                        # GAP 1: Session hydration events
                        ("session.hydrated", self._on_session_hydrated),
                        # C2: Dashboard events
                        ("plan.progress", self._on_plan_progress_ui),
                        ("tool.execute.finish", self._on_tool_finish_ui),
                        ("tool.execute.error", self._on_tool_error_ui),
                        # M4: Diff preview before file edits
                        ("file.diff.preview", self._on_diff_preview_ui),
                        # M1: Real-time streaming token output
                        ("model.token", self._on_model_token_ui),
                    ]
                    for event_name, cb in _subs:
                        eb.subscribe(event_name, cb)
                        self._eb_subscriptions.append((event_name, cb))

                    # GAP 1: Request session state hydration on mount
                    eb.publish("session.request_state", {"session_id": "default"})
            except Exception:
                pass

            # Initial state pull
            try:
                # If orchestrator doesn't have an adapter, try to pick one from PM
                if not self.orchestrator.adapter:
                    pm = get_provider_manager()
                    provs = pm.list_providers()
                    if provs:
                        name = "lm_studio" if "lm_studio" in provs else provs[0]
                        self.orchestrator.adapter = pm.get_provider(name)

                if self.orchestrator.adapter:
                    adapter = self.orchestrator.adapter
                    if hasattr(adapter, "provider") and isinstance(
                        adapter.provider, dict
                    ):
                        self._current_provider = (
                            adapter.provider.get("name")
                            or adapter.provider.get("type")
                            or "None"
                        )
                    if (
                        hasattr(adapter, "models")
                        and isinstance(adapter.models, list)
                        and adapter.models
                    ):
                        self._current_model = adapter.models[0]
                    elif hasattr(adapter, "default_model") and adapter.default_model:
                        self._current_model = adapter.default_model
                    self._update_provider_model_label()
                else:
                    await self._refresh_provider_info()
            except Exception:
                await self._refresh_provider_info()

            if hasattr(self, "_refresh_task_state"):
                self._refresh_task_state()

        async def _refresh_provider_info(self) -> None:
            # read provider manager for basic info
            try:
                pm = get_provider_manager()
                providers = pm.list_providers()
                if providers:
                    # Pick LM Studio if available, else first
                    prov = "lm_studio" if "lm_studio" in providers else providers[0]
                    self._set_provider(prov)

                    # Force a probe for models if needed
                    models = pm.get_cached_models(prov)
                    if not models and hasattr(
                        self.orchestrator.adapter, "get_models_from_api"
                    ):
                        try:
                            # Run in thread since it might be slow
                            def probe():
                                pm.get_models_from_api(prov)  # type: ignore[reportAttributeAccessIssue]
                                # trigger UI refresh on bus
                                self.event_bus.publish(  # type: ignore[reportOptionalMemberAccess]
                                    "provider.models.list",
                                    {
                                        "provider": prov,
                                        "models": pm.get_cached_models(prov),
                                    },
                                )

                            threading.Thread(target=probe, daemon=True).start()
                        except Exception:
                            pass

                    if models:
                        self._set_model(models[0])
            except Exception as e:
                guilogger.error(f"TextualApp: _refresh_provider_info failed: {e}")

        def _update_provider_model_label(self):
            if self.provider_model_label:
                self.provider_model_label.update(
                    f"Provider: {self._current_provider} | Model: {self._current_model}"
                )

        def _set_provider(self, name: str) -> None:
            self._current_provider = name
            self._update_provider_model_label()
            try:
                from src.core.user_prefs import UserPrefs

                prefs = UserPrefs.load()
                prefs.selected_model_provider = name
                prefs.save()

                pm = get_provider_manager()
                if pm:
                    adapter = pm.get_provider(name)
                    if adapter:
                        self.orchestrator.adapter = adapter
            except Exception as e:
                guilogger.error(f"Error setting provider: {e}")

        def _set_model(self, name: str) -> None:
            self._current_model = name
            self._update_provider_model_label()
            try:
                from src.core.user_prefs import UserPrefs

                prefs = UserPrefs.load()
                prefs.selected_model_name = name
                prefs.save()
            except Exception:
                pass

        def _on_provider_status_changed(self, payload) -> None:
            try:
                prov = payload.get("provider")
                status = payload.get("status")
                if prov:
                    self._current_provider = f"{prov} ({status})"
                    self._update_provider_model_label()
            except Exception:
                pass

        def _on_model_routing(self, payload) -> None:
            try:
                prov = payload.get("provider")
                model = payload.get("selected")
                if prov:
                    self._current_provider = prov
                if model:
                    self._current_model = model
                self._update_provider_model_label()
            except Exception:
                pass

        def _on_provider_models(self, payload) -> None:
            try:
                prov = payload.get("provider")
                models = payload.get("models")
                if prov and models:
                    self._set_provider(prov)
                    self._set_model(models[0] if models else "None")
            except Exception:
                pass

        def _on_log_new(self, payload) -> None:
            try:
                if self.sys_log:
                    self.sys_log.write(
                        f"[{payload.get('timestamp')}] [{payload.get('level')}] {payload.get('message')}"
                    )
            except Exception:
                pass

        async def on_input_submitted(self, event: Input.Submitted) -> None:
            raw_text = event.value
            if not raw_text:
                return

            # Clear input immediately for responsive feel
            event.input.value = ""

            # 1. Visually Echo the user's prompt to the Chat Log IMMEDIATELY
            if self.output:
                try:
                    from rich.text import Text

                    self.output.write(
                        Text.from_markup(f"[bold][blue]User:[/blue][/bold] {raw_text}")
                    )
                except Exception:
                    self._safe_write(f"User: {raw_text}")

            # Record in history
            if isinstance(self.input_widget, ChatInput):
                if (
                    not self.input_widget.history
                    or self.input_widget.history[-1] != raw_text
                ):
                    self.input_widget.history.append(raw_text)
                self.input_widget.history_index = -1

            # Unescape compact newlines
            text = raw_text.replace("\\n", "\n")

            # Handle "continue" command
            if text.strip().lower() == "continue":
                if self._continue_state:
                    if self._restore_state_for_continue():
                        self._safe_write("[cyan]⟳ Resuming from saved state...[/cyan]")
                        # Get the last user message from history to continue
                        history = self._continue_state.get("history", [])
                        last_user_msg = None
                        for m in reversed(history):
                            if m.get("role") == "user":
                                last_user_msg = m.get("content", "")
                                break
                        if last_user_msg:
                            text = last_user_msg
                            self._continue_state = None
                        else:
                            self._safe_write(
                                "[yellow]No previous task found. Please enter a new command.[/yellow]"
                            )
                            if self.input_widget:
                                self.input_widget.value = ""
                            return
                    else:
                        self._safe_write(
                            "[yellow]No saved state to continue from.[/yellow]"
                        )
                        if self.input_widget:
                            self.input_widget.value = ""
                        return
                else:
                    self._safe_write(
                        "[yellow]No saved state to continue from.[/yellow]"
                    )
                    if self.input_widget:
                        self.input_widget.value = ""
                    return

            # Handle slash commands
            stripped = text.strip()
            if stripped.startswith("/") and stripped.lower() != "/continue":
                cmd_parts = stripped.split(None, 1)
                cmd = cmd_parts[0].lower()
                cmd_arg = cmd_parts[1] if len(cmd_parts) > 1 else ""

                if cmd == "/quit":
                    self.action_quit_app()
                    return

                elif cmd == "/new" or cmd == "/reset":
                    controller = getattr(self, "controller", None)
                    _do_new_session(self, controller)
                    return

                elif cmd == "/compact":

                    def _run_compact():
                        try:
                            _do_compact_session(self)
                        except Exception as exc:
                            self._safe_write(f"[red]Compact failed: {exc}[/red]")

                    threading.Thread(target=_run_compact, daemon=True).start()
                    return

                elif cmd == "/clear":
                    if self.output:
                        self.output.clear()
                    return

                elif cmd == "/settings":
                    self.action_open_settings()
                    return

                elif cmd == "/interrupt":
                    self.action_interrupt_agent()
                    return

                elif cmd == "/status":
                    running = "running" if self._agent_running else "idle"
                    orch = getattr(self, "orchestrator", None)
                    provider = "unknown"
                    if orch:
                        try:
                            provider = getattr(orch, "provider_name", None) or getattr(
                                orch, "_provider_name", "unknown"
                            )
                        except Exception:
                            pass
                    msg_count = 0
                    if orch and hasattr(orch, "msg_mgr"):
                        msg_count = len(getattr(orch.msg_mgr, "messages", []))
                    self._safe_write(
                        f"[dim]Status: agent={running}, provider={provider}, messages={msg_count}[/dim]"
                    )
                    return

                elif cmd == "/help":
                    lines = [
                        "[bold]Available commands:[/bold]",
                        "  /help          — show this message",
                        "  /clear         — clear chat output",
                        "  /compact       — compact context to prose (frees token budget)",
                        "  /new           — start a new session (clears history)",
                        "  /reset         — same as /new",
                        "  /continue      — resume from last saved state",
                        "  /interrupt     — interrupt running agent",
                        "  /settings      — open settings modal",
                        "  /status        — show agent + provider status",
                        "  /provider [n]  — show or switch provider",
                        "  /model [n]     — show or switch model",
                        "  /quit          — quit and save session to vector DB",
                    ]
                    for line in lines:
                        self._safe_write(line)
                    return

                elif cmd == "/provider":
                    orch = getattr(self, "orchestrator", None)
                    if not cmd_arg:
                        # List providers
                        try:
                            import json
                            import pathlib

                            pf = pathlib.Path("src/config/providers.json")
                            if pf.exists():
                                providers = json.loads(pf.read_text())
                                names = [p.get("name", "?") for p in providers]
                                self._safe_write(
                                    f"[dim]Providers: {', '.join(names)}[/dim]"
                                )
                            else:
                                self._safe_write("[dim]No providers.json found.[/dim]")
                        except Exception as e:
                            self._safe_write(f"[red]Error listing providers: {e}[/red]")
                    else:
                        self._safe_write(
                            "[dim]Use the Settings screen (Ctrl+O) to switch providers.[/dim]"
                        )
                    return

                elif cmd == "/model":
                    orch = getattr(self, "orchestrator", None)
                    if not cmd_arg:
                        model = "unknown"
                        if orch:
                            try:
                                model = getattr(orch, "model", None) or getattr(
                                    orch, "_model", "unknown"
                                )
                            except Exception:
                                pass
                        self._safe_write(f"[dim]Current model: {model}[/dim]")
                    else:
                        self._safe_write(
                            "[dim]Use the Settings screen (Ctrl+O) to switch models.[/dim]"
                        )
                    return

                # Unknown slash command — fall through to orchestrator

            # display in output immediately
            # NOTE: User message already echoed above (line 577), don't duplicate
            # if self.output:
            #     self._safe_write(f"User: {text}")

            # Update sidebar task state immediately with the new task
            if hasattr(self, "task_state_label") and self.task_state_label:
                # Truncate long tasks for display
                display_task = (
                    text.strip()[:80] + "..."
                    if len(text.strip()) > 80
                    else text.strip()
                )
                guilogger.info(f"Updating task_state_label to: {display_task}")
                self.task_state_label.update(f"▶ {display_task}")
            else:
                guilogger.warning(
                    f"task_state_label not available: hasattr={hasattr(self, 'task_state_label')}"
                )

            # Show progress indicator
            self._show_progress()

            # send to orchestrator in background
            # Fix: target _run_agent directly — send_prompt would spawn a second thread
            with self._agent_lock:
                if self._agent_running:
                    guilogger.warning(
                        "on_input_submitted: agent already running, ignoring duplicate submission"
                    )
                    return
                self._agent_running = True
            with self._history_lock:
                self.history.append(("user", text))
            self._agent_thread = threading.Thread(
                target=self._run_agent, args=(text,), daemon=True
            )
            self._agent_thread.start()
            # clear input
            if self.input_widget:
                self.input_widget.value = ""

        def _show_progress(self) -> None:
            """Show progress indicator in the UI."""
            try:
                # Show spinner in mode label
                if self.mode_label:
                    self.mode_label.update("🔄 Working...")
            except Exception as e:
                guilogger.error(f"Failed to show progress: {e}")

        def _hide_progress(self) -> None:
            """Hide progress indicator."""
            try:
                if self.mode_label:
                    self.mode_label.update("Idle")
            except Exception:
                pass

        def on_agent_result(self, content: str) -> None:
            # called from background thread via call_from_thread
            # Note: _agent_running is cleared by the finally block in _run_agent;
            # clearing it here again would be a race if done without the lock.
            self._cancel_event.clear()
            self._hide_progress()
            try:
                # Schedule append on the main loop (safe wrapper)
                self._schedule_callback(self._append_assistant, content)
            except Exception:
                try:
                    self._append_assistant(content)
                except Exception:
                    pass

        def _append_assistant(self, content: str) -> None:
            if not self.output:
                return

            # Check if content starts with tool result indicator (emoji prefix)
            # Tool results should not be prefixed with "Assistant:"
            is_tool_result = content.strip().startswith(
                ("📁", "📄", "📂", "📝", "✅", "❌", "⚠️", "🔍", "🔧", "✏️")
            )

            # NOTE: Diff rendering is handled by _on_diff_preview_ui via events.
            # Do NOT parse diffs from message content - that causes duplicate display.
            # Tool results now contain summary only, not raw diffs.

            thinking_match = _THINKING_PATTERN.search(content)

            if thinking_match:
                thinking = thinking_match.group(1).strip()
                rest = content[thinking_match.end() :].strip()

                try:
                    from rich.text import Text

                    self.output.write(
                        Text.from_markup("[dim italic]Thinking:[/dim italic]")
                    )
                    self.output.write(Text.from_markup(f"[dim]{thinking}[/dim]"))
                    self.output.write("")
                except Exception:
                    self.output.write("Thinking:")
                    self.output.write(thinking)
                    self.output.write("")

                if rest:
                    try:
                        from rich.text import Text

                        self.output.write(
                            Text.from_markup(f"[bold]Assistant:[/bold] {rest}")
                        )
                    except Exception:
                        self._safe_write(f"Assistant: {rest}")
            else:
                # If content starts with tool result emoji, display without "Assistant:" prefix
                if is_tool_result:
                    try:
                        from rich.text import Text

                        self.output.write(Text.from_markup(content))
                    except Exception:
                        self._safe_write(content)
                else:
                    try:
                        from rich.text import Text

                        self.output.write(
                            Text.from_markup(f"[bold]Assistant:[/bold] {content}")
                        )
                    except Exception:
                        self._safe_write(f"Assistant: {content}")

            if hasattr(self, "task_state_label"):
                self._refresh_task_state()

        def _render_side_by_side_diff(self, diff_content: str) -> None:
            """Render a side-by-side diff view using Rich tables."""
            import re as _re
            import difflib

            clean_diff = _re.sub(
                r"\[/?(?:bold|dim|italic|cyan|red|green|blue).*?\]", "", diff_content
            )

            try:
                from rich.table import Table
                from rich.text import Text

                lines = clean_diff.strip().split("\n")

                # Parse unified diff into old/new lines
                old_lines = []
                new_lines = []
                old_line_num = 0
                new_line_num = 0
                in_hunk = False

                for line in lines:
                    if line.startswith("@@"):
                        in_hunk = True
                        # Parse @@ -old_start,old_count +new_start,new_count @@
                        match = _re.match(r"@@ -(\d+),?\d* \+(\d+),?\d* @@", line)
                        if match:
                            old_line_num = int(match.group(1))
                            new_line_num = int(match.group(2))
                    elif in_hunk and len(line) > 0:
                        # Skip file headers (--- a.txt, +++ b.txt)
                        if line.startswith("---") or line.startswith("+++"):
                            continue
                        elif line.startswith("-"):
                            old_lines.append((old_line_num, line[1:].rstrip()))
                            old_line_num += 1
                        elif line.startswith("+"):
                            new_lines.append((new_line_num, line[1:].rstrip()))
                            new_line_num += 1
                        elif line.startswith(" "):
                            text = line[1:].rstrip()
                            old_lines.append((old_line_num, text))
                            new_lines.append((new_line_num, text))
                            old_line_num += 1
                            new_line_num += 1
                        else:
                            # Context line without space prefix
                            old_lines.append((old_line_num, line.rstrip()))
                            new_lines.append((new_line_num, line.rstrip()))
                            old_line_num += 1
                            new_line_num += 1

                if not old_lines and not new_lines:
                    # No hunk info, just show colored diff
                    for line in clean_diff.split("\n"):
                        if line.startswith("@@"):
                            self.output.write(Text(line, style="cyan bold"))  # type: ignore[reportOptionalMemberAccess]
                        elif line.startswith("+"):
                            self.output.write(Text(line, style="green"))  # type: ignore[reportOptionalMemberAccess]
                        elif line.startswith("-"):
                            self.output.write(Text(line, style="red"))  # type: ignore[reportOptionalMemberAccess]
                        else:
                            self.output.write(line)  # type: ignore[reportOptionalMemberAccess]
                    return

                # Build side-by-side table
                table = Table(show_header=True, show_edge=False, pad_edge=False)
                table.add_column("OLD", style="red", width=35)
                table.add_column("NEW", style="green", width=35)

                old_idx = 0
                new_idx = 0

                while old_idx < len(old_lines) or new_idx < len(new_lines):
                    old_cell = ""
                    new_cell = ""

                    if old_idx < len(old_lines):
                        num, text = old_lines[old_idx]
                        old_cell = f"{num:4d} │ {text}"
                        old_idx += 1

                    if new_idx < len(new_lines):
                        num, text = new_lines[new_idx]
                        new_cell = f"{num:4d} │ {text}"
                        new_idx += 1

                    table.add_row(old_cell, new_cell)

                self.output.write("")  # type: ignore[reportOptionalMemberAccess]
                self.output.write(table)  # type: ignore[reportOptionalMemberAccess]
                self.output.write("")  # type: ignore[reportOptionalMemberAccess]

            except Exception as e:
                # Fallback to simple colored diff
                from rich.text import Text

                for line in clean_diff.split("\n"):
                    if line.startswith("@@"):
                        self.output.write(Text(line, style="cyan bold"))  # type: ignore[reportOptionalMemberAccess]
                    elif line.startswith("+"):
                        self.output.write(Text(line, style="green"))  # type: ignore[reportOptionalMemberAccess]
                    elif line.startswith("-"):
                        self.output.write(Text(line, style="red"))  # type: ignore[reportOptionalMemberAccess]
                    else:
                        self.output.write(line)  # type: ignore[reportOptionalMemberAccess]

        def _refresh_task_state(self):
            try:
                import os

                wd = getattr(self.orchestrator, "working_dir", None) or os.getcwd()
                from pathlib import Path

                task_state_path = Path(wd) / ".agent-context" / "TASK_STATE.md"

                # Check if there's an active session by looking at message history
                has_active_session = False
                try:
                    if hasattr(self.orchestrator, "msg_mgr"):
                        msgs = self.orchestrator.msg_mgr.messages
                        # If there are user messages, there's an active session
                        user_msgs = [m for m in msgs if m.get("role") == "user"]
                        has_active_session = len(user_msgs) > 0
                except Exception:
                    pass

                if not has_active_session:
                    # Fresh start - show "No active task"
                    if hasattr(self, "task_state_label"):
                        self.task_state_label.update("No active task")
                    return

                if task_state_path.exists():
                    content = task_state_path.read_text()
                    guilogger.info(f"Task State read: {content[:100]}...")
                    lines = content.splitlines()
                    task_info = ""
                    capture = False
                    for line in lines:
                        if line.startswith("# Current Task"):
                            capture = True
                            continue
                        if capture and line.startswith("#"):
                            break
                        if capture and line.strip():
                            task_info = line.strip()
                            break

                    # Only update if there's actual task info (not the stale "completed" state)
                    if task_info and "completed" not in task_info.lower():
                        guilogger.info(f"Task State label updating to: {task_info}")
                        self.task_state_label.update(task_info)
                    else:
                        guilogger.info(
                            f"Task State label keeping current (stale task info: {task_info})"
                        )
            except Exception as e:
                guilogger.error(f"Failed to refresh task state: {e}")

        def _on_token_usage(self, payload) -> None:
            guilogger.info(f"Token usage event received: {payload}")
            if not self.context_label:
                guilogger.warning("Token usage: no context_label available")
                return
            used = payload.get("total_tokens", 0)
            prompt = payload.get("prompt_tokens", 0)
            completion = payload.get("completion_tokens", 0)
            latency = payload.get("latency", 0)
            model = payload.get("model", "N/A")
            self.context_label.update(
                f"Model: {model}\nUsed: {used}\nPrompt: {prompt}\nReply: {completion}\nLatency: {latency:.2f}s"
            )

        def _on_session_new(self, payload) -> None:
            guilogger.info(f"New session started: {payload}")
            try:
                import os

                wd = getattr(self.orchestrator, "working_dir", None) or os.getcwd()
                from pathlib import Path

                task_state_path = Path(wd) / ".agent-context" / "TASK_STATE.md"

                # Offload blocking I/O off the EventBus publisher thread.
                def _write_task_state(p=task_state_path):
                    try:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(
                            "# Current Task\n\n# Completed Steps\n\n# Next Step\n"
                        )
                    except Exception as _e:
                        pass

                import threading as _thr

                _thr.Thread(target=_write_task_state, daemon=True).start()
                guilogger.info("Task state cleared for new session")
                if hasattr(self, "task_state_label"):
                    self.task_state_label.update("No active task")
                if hasattr(self, "context_label") and self.context_label:
                    self.context_label.update(
                        "Model: --\nUsed: 0\nPrompt: 0\nReply: 0\nLatency: 0.00s"
                    )
            except Exception as e:
                guilogger.error(f"Failed to clear task state on new session: {e}")

        def _on_session_hydrated(self, payload: Dict[str, Any]) -> None:
            """GAP 1: Handle session state hydration - render UI from full state."""
            guilogger.info(f"Session hydrated: {payload}")
            try:
                # Render message history
                if "messageHistory" in payload and payload["messageHistory"]:
                    for msg in payload["messageHistory"]:
                        role = msg.get("role", "assistant")
                        content = msg.get("content", "")
                        if role == "user" and content:
                            if self.output:
                                try:
                                    from rich.text import Text

                                    self.output.write(
                                        Text.from_markup(
                                            f"[bold][blue]User:[/blue][/bold] {content}"
                                        )
                                    )
                                except Exception:
                                    pass
                        elif role == "assistant" and content:
                            if self.output:
                                try:
                                    from rich.text import Text

                                    self.output.write(
                                        Text.from_markup(
                                            f"[bold]Assistant:[/bold] {content}"
                                        )
                                    )
                                except Exception:
                                    pass

                # Render current plan
                if "currentPlan" in payload:
                    plan = payload["currentPlan"]
                    steps = plan.get("steps", [])
                    current_step = plan.get("currentStep", 0)
                    if steps and hasattr(self, "plan_progress_label"):
                        step_desc = (
                            steps[current_step].get("description", "")
                            if current_step < len(steps)
                            else ""
                        )
                        self.plan_progress_label.update(  # type: ignore[reportOptionalMemberAccess]
                            f"○ Step {current_step + 1}/{len(steps)}\n{step_desc[:40]}"
                        )

                # Render provider info
                if "provider" in payload:
                    prov = payload["provider"]
                    prov_name = prov.get("name", "None")
                    model_name = prov.get("model", "None")
                    self._current_provider = prov_name
                    self._current_model = model_name
                    self._update_provider_model_label()

                # Render token budget
                if "tokenBudget" in payload:
                    tb = payload["tokenBudget"]
                    used = tb.get("used_tokens", 0)
                    max_tok = tb.get("max_tokens", 0)
                    if max_tok > 0 and hasattr(self, "context_label"):
                        pct = (used / max_tok) * 100
                        self.context_label.update(f"Used: {used}/{max_tok}\n{pct:.1f}%")  # type: ignore[reportOptionalMemberAccess]

                # Render workspace state
                if "workspace" in payload:
                    ws = payload["workspace"]
                    files_read = ws.get("filesRead", [])
                    files_mod = ws.get("filesModified", [])
                    if hasattr(self, "tool_activity_label"):
                        if files_mod:
                            last_file = files_mod[-1].split("/")[-1]
                            self.tool_activity_label.update(f"✓ {last_file}")  # type: ignore[reportOptionalMemberAccess]
                        elif files_read:
                            last_file = files_read[-1].split("/")[-1]
                            self.tool_activity_label.update(f"○ {last_file}")  # type: ignore[reportOptionalMemberAccess]

                guilogger.info("TUI rendered from hydrated session state")
            except Exception as e:
                guilogger.error(f"Failed to render hydrated state: {e}")

        async def on_unmount(self) -> None:
            """H7: Unsubscribe all EventBus callbacks to prevent memory leaks."""
            try:
                eb = get_event_bus()
                if eb:
                    for event_name, cb in self._eb_subscriptions:
                        try:
                            eb.unsubscribe(event_name, cb)
                        except Exception:
                            pass
                    self._eb_subscriptions.clear()
            except Exception:
                pass
            # Signal cancel event so agent thread exits cleanly
            try:
                self._cancel_event.set()
            except Exception:
                pass
            # Stop audit log worker thread
            try:
                from src.core.logger import _audit_stop

                _audit_stop.set()  # type: ignore[reportOptionalMemberAccess]
            except Exception:
                pass
            # Shut down memory_update_node ThreadPoolExecutor (non-blocking)
            try:
                from src.core.orchestration.graph.nodes import (
                    memory_update_node as _mun,
                )

                if hasattr(_mun, "_executor"):
                    _mun._executor.shutdown(wait=False)
            except Exception:
                pass

        # C2: Dashboard event handlers — update sidebar plan progress and tool activity labels

        def _on_plan_progress_ui(self, payload: Dict[str, Any]) -> None:
            """Update the plan progress sidebar label from plan.progress events (GAP 2: ACP schema)."""
            try:
                if not self.plan_progress_label or not isinstance(payload, dict):
                    return
                # GAP 2: ACP schema - use currentStep/totalSteps/stepDescription
                step = payload.get("currentStep", payload.get("step", 0))
                total = payload.get("totalSteps", payload.get("total", 0))
                desc = payload.get("stepDescription", payload.get("description", ""))
                if total:
                    bar = "█" * step + "░" * (total - step)
                    desc_display = (desc[:38] + "…") if len(desc) > 40 else desc
                    text = (
                        f"Step {step}/{total}\n{bar}\n{desc_display}"
                        if desc
                        else f"Step {step}/{total}\n{bar}"
                    )
                else:
                    text = desc[:40] if desc else "Running…"
                self._schedule_callback(self.plan_progress_label.update, text)
            except Exception:
                pass

        def _on_tool_finish_ui(self, payload: Dict[str, Any]) -> None:
            """Update the tool activity sidebar label on tool completion (GAP 2: ACP schema)."""
            try:
                if not self.tool_activity_label or not isinstance(payload, dict):
                    return
                # GAP 2: ACP schema - use title instead of tool, status instead of ok
                tool = payload.get("title", payload.get("tool", "?"))
                status = payload.get("status", "completed")
                status_symbol = "✓" if status == "completed" else "✗"
                self._schedule_callback(
                    self.tool_activity_label.update, f"{status_symbol} {tool}"
                )
            except Exception:
                pass

        def _on_tool_error_ui(self, payload: Dict[str, Any]) -> None:
            """Update the tool activity sidebar label on tool error (GAP 2: ACP schema)."""
            try:
                if not self.tool_activity_label or not isinstance(payload, dict):
                    return
                # GAP 2: ACP schema - use title instead of tool
                tool = payload.get("title", payload.get("tool", "?"))
                error = payload.get("error", "")
                error_preview = f" ({error[:20]}...)" if error else ""
                self._schedule_callback(
                    self.tool_activity_label.update, f"✗ {tool}{error_preview}"
                )
            except Exception:
                pass

        def _on_model_token_ui(self, payload: Dict[str, Any]) -> None:
            """M1: Append streaming tokens from the LLM to the output log in real time.

            Receives model.token events from _consume_sse_stream in llm_manager.py.
            partial=True  → incremental token chunk (append without newline)
            partial=False → stream complete (flush / no-op since chunks already written)
            """
            try:
                if not self.output or not isinstance(payload, dict):
                    return
                if not payload.get("partial", True):
                    # Stream complete — write trailing newline so next output starts fresh
                    self._schedule_callback(self.output.write, "")
                    return
                token_text = payload.get("text", "")
                if token_text:
                    # Use _schedule_callback to safely write from the background thread
                    self._schedule_callback(self.output.write, token_text)
            except Exception:
                pass

        def _on_diff_preview_ui(self, payload: Dict[str, Any]) -> None:
            """M4: Show diff preview in the output log before/after a file write.

            Receives file.diff.preview events published by write_file / edit_file /
            edit_by_line_range in file_tools.py.
            """
            try:
                from rich.text import Text

                if not self.output or not isinstance(payload, dict):
                    return
                path = payload.get("path", "?")
                diff = payload.get("diff", "")
                is_new = payload.get("is_new_file", False)
                if not diff:
                    return

                # Show file info header
                label = "🆕 New file" if is_new else "✏️  Edit"
                header = f"[bold][cyan]{label}:[/cyan][/bold] [dim]{path}[/dim]"

                # Compute truncation info once so both render paths can use it
                total_lines = len(diff.splitlines())
                if total_lines > 60:
                    self._schedule_callback(
                        self.output.write,
                        f"[dim]… {total_lines - 60} more lines not shown[/dim]",
                    )

                # Try side-by-side diff first, fallback to colored
                try:
                    self._render_side_by_side_diff(diff)
                    return
                except Exception:
                    pass

                # Fallback: colored unified diff
                coloured_lines = []
                for line in diff.splitlines()[:60]:
                    if line.startswith("+") and not line.startswith("+++"):
                        coloured_lines.append(f"[green]{line}[/green]")
                    elif line.startswith("-") and not line.startswith("---"):
                        coloured_lines.append(f"[red]{line}[/red]")
                    elif line.startswith("@@"):
                        coloured_lines.append(f"[cyan]{line}[/cyan]")
                    else:
                        coloured_lines.append(f"[dim]{line}[/dim]")

                if total_lines > 60:
                    coloured_lines.append(f"[dim]… {total_lines - 60} more lines[/dim]")

                diff_block = "\n".join(coloured_lines)
                text = Text.from_markup(f"{header}\n{diff_block}")
                self._schedule_callback(self.output.write, text)
            except Exception:
                pass

        def action_toggle_log(self) -> None:
            if self.sys_log:
                self.sys_log.display = not self.sys_log.display

        def action_open_settings(self) -> None:
            # U4: Guard against _settings_modal not yet initialised (compose() not yet run)
            if not getattr(self, "_settings_modal", None):
                guilogger.warning(
                    "action_open_settings: settings modal not yet initialised"
                )
                return
            try:
                providers = []
                try:
                    pm = get_provider_manager()
                    providers = list(pm.list_providers())
                except Exception:
                    providers = []
                self._settings_modal.open_with(  # type: ignore[reportAttributeAccessIssue]
                    providers, providers[0] if providers else None
                )
                self.push_screen(self._settings_modal)  # type: ignore[reportAttributeAccessIssue]
            except Exception as e:
                guilogger.error(f"Failed to open settings: {e}")

        def action_quit_app(self) -> None:
            """Gracefully quit: save session, clear state files, stop background resources."""
            guilogger.info("Quit requested — shutting down")

            # Save session to vector DB and clear state files before exit
            self._save_and_clear_session()

            # Signal cancel to any running agent
            try:
                self._cancel_event.set()
            except Exception:
                pass
            # Stop audit log worker
            try:
                from src.core.logger import _audit_stop

                _audit_stop.set()  # type: ignore[reportOptionalMemberAccess]
            except Exception:
                pass
            # Shut down memory_update_node executor (non-blocking)
            try:
                from src.core.orchestration.graph.nodes import (
                    memory_update_node as _mun,
                )

                if hasattr(_mun, "_executor"):
                    _mun._executor.shutdown(wait=False)
            except Exception:
                pass
            self.exit()

        def _save_and_clear_session(self) -> None:
            """Save session data to vector DB and clear state files for fresh start."""
            try:
                from src.core.indexing.vector_store import VectorStore
                from pathlib import Path

                # Get session ID and working directory
                task_id = getattr(self.orchestrator, "_current_task_id", "unknown")
                working_dir = getattr(self.orchestrator, "working_dir", None)
                if not working_dir:
                    working_dir = Path.cwd()
                else:
                    working_dir = Path(working_dir)

                agent_context = working_dir / ".agent-context"

                # Read current session data
                session_data = {
                    "session_id": task_id,
                    "task_state": "",
                    "todo_content": "",
                    "plan_content": "",
                    "timestamp": str(Path(__file__).stat().st_mtime) if False else "",
                }

                # Read TODO.md if exists
                todo_path = agent_context / "TODO.md"
                if todo_path.exists():
                    session_data["todo_content"] = todo_path.read_text()

                # Read TASK_STATE.md if exists
                task_state_path = agent_context / "TASK_STATE.md"
                if task_state_path.exists():
                    session_data["task_state"] = task_state_path.read_text()

                # Read last_plan.json if exists
                last_plan_path = agent_context / "last_plan.json"
                if last_plan_path.exists():
                    session_data["plan_content"] = last_plan_path.read_text()

                # Add to vector store for semantic search
                try:
                    vector_store = VectorStore(str(working_dir))
                    session_summary = (
                        f"Session {task_id}: {session_data.get('task_state', '')[:500]}"
                    )
                    vector_store.add_memory(
                        session_summary, {"type": "session", "session_id": task_id}
                    )
                    guilogger.info(f"Saved session {task_id} to vector store")
                except Exception as e:
                    guilogger.debug(f"Could not save to vector store: {e}")

                # Save to session store in SQLite
                try:
                    if hasattr(self.orchestrator, "session_store"):
                        store = self.orchestrator.session_store
                        # Add plan if exists
                        if session_data.get("plan_content"):
                            store.add_plan(
                                task_id,
                                session_data["plan_content"][:2000],
                                "completed",
                            )
                        guilogger.info(f"Saved session {task_id} to session store")
                except Exception as e:
                    guilogger.debug(f"Could not save to session store: {e}")

                # Clear state files for fresh start
                files_to_clear = [
                    agent_context / "TODO.md",
                    agent_context / "TASK_STATE.md",
                    agent_context / "last_plan.json",
                    agent_context / "execution_trace.json",
                    agent_context / "usage.json",
                ]

                for f in files_to_clear:
                    if f.exists():
                        f.write_text("")
                        guilogger.debug(f"Cleared {f.name}")

                guilogger.info(f"Session {task_id} saved and state files cleared")

            except Exception as e:
                guilogger.error(f"Session cleanup failed: {e}")

        def action_interrupt_agent(self) -> None:
            """Interrupt the currently running agent."""
            # Check if agent is running - more robust check
            with self._agent_lock:
                running = self._agent_running
            if running:
                thread_alive = self._agent_thread and self._agent_thread.is_alive()
                if thread_alive:
                    guilogger.info("User interrupted agent (Escape pressed)")
                    self._cancel_event.set()
                    self.output.write(  # type: ignore[reportOptionalMemberAccess]
                        "[yellow]⚠ Agent interrupted. Type 'continue' to resume or enter a new command.[/yellow]"
                    )
                    with self._agent_lock:
                        self._agent_running = False
                    return
            guilogger.info("No agent running to interrupt")

        def action_force_interrupt_agent(self) -> None:
            """Force interrupt the agent immediately (double-ESC)."""
            guilogger.info("User force interrupted agent (double-escape pressed)")
            self._cancel_event.set()
            with self._agent_lock:
                running = self._agent_running
                if running:
                    self._agent_running = False
            if running:
                self.output.write(  # type: ignore[reportOptionalMemberAccess]
                    "[red]⚠ Agent force stopped. Type 'continue' to resume or enter a new command.[/red]"
                )
            if self._agent_thread and self._agent_thread.is_alive():
                guilogger.info("Agent thread still running, setting cancel event")

        def _save_state_for_continue(self) -> None:
            """Save current agent state for continue functionality."""
            try:
                if self.orchestrator and hasattr(self.orchestrator, "msg_mgr"):
                    msg_mgr = self.orchestrator.msg_mgr
                    # U6: persist full AgentState fields so continue can resume mid-plan
                    agent_state = (
                        getattr(self.orchestrator, "_last_agent_state", {}) or {}
                    )
                    self._continue_state = {
                        "history": msg_mgr.messages.copy()
                        if hasattr(msg_mgr, "messages")
                        else [],
                        "session_read_files": list(
                            getattr(self.orchestrator, "_session_read_files", [])
                        ),
                        "current_plan": agent_state.get("current_plan"),
                        "current_step": agent_state.get("current_step"),
                        "working_dir": agent_state.get("working_dir"),
                        "step_retry_counts": agent_state.get("step_retry_counts"),
                    }
                    guilogger.info("State saved for continue")
            except Exception as e:
                guilogger.error(f"Failed to save state for continue: {e}")

        def _restore_state_for_continue(self) -> bool:
            """Restore saved state for continue functionality."""
            if not self._continue_state:
                return False
            try:
                if self.orchestrator and hasattr(
                    self.orchestrator, "restore_continue_state"
                ):
                    self.orchestrator.restore_continue_state(self._continue_state)
                    guilogger.info("State restored for continue")
                    return True
            except Exception as e:
                guilogger.error(f"Failed to restore state: {e}")
            return False

    # Settings modal — reuse names from the runtime import block above

    # ---------------------------------------------------------------------------
    # Session action helpers (module-level so tests can assert on source text)
    # ---------------------------------------------------------------------------

    def _do_new_session(app, controller=None) -> None:
        """New session: saves current session to vector DB, clears state files, starts fresh."""
        # Save and clear current session before starting new one
        try:
            if hasattr(app, "_save_and_clear_session"):
                app._save_and_clear_session()
        except Exception:
            pass

        # 1. Publish session.new event → writes blank TASK_STATE.md, resets context label
        if controller:
            try:
                controller.start_new_session()
            except Exception:
                pass
        # 2. Clear orchestrator state (message history + session read tracking)
        # start_new_task() already resets _session_read_files and all per-task state.
        try:
            orch = getattr(app, "orchestrator", None)
            if orch and hasattr(orch, "start_new_task"):
                orch.start_new_task()
        except Exception:
            pass
        # 3. Clear UI input history and output panel
        if hasattr(app, "history"):
            app.history.clear()
        if hasattr(app, "output") and app.output:
            app.output.clear()
            app.output.write("[dim]New session started.[/dim]")

    def _do_compact_session(app) -> None:
        """Compact message history to a prose summary via the distiller."""
        try:
            from src.core.memory.distiller import compact_messages_to_prose

            orch = getattr(app, "orchestrator", None)
            msg_mgr = getattr(orch, "msg_mgr", None) if orch else None
            if msg_mgr and getattr(msg_mgr, "messages", None):
                msgs = list(msg_mgr.messages[-40:])
                n_before = len(msg_mgr.messages)
                prose = compact_messages_to_prose(msgs)
                msg_mgr.messages = [{"role": "user", "content": prose}]
                if hasattr(app, "_safe_write"):
                    app._safe_write(
                        f"[dim]Context compacted: {n_before} → 1 message.[/dim]"
                    )
            else:
                if hasattr(app, "_safe_write"):
                    app._safe_write("[dim]Nothing to compact.[/dim]")
        except Exception as e:
            if hasattr(app, "_safe_write"):
                app._safe_write(f"[red]Compact failed: {e}[/red]")

    # ---------------------------------------------------------------------------
    # Compact single-screen settings modal — no nested provider/model screens
    # ---------------------------------------------------------------------------

    class SettingsModal(ModalScreen):
        """Compact settings panel: session actions + inline provider/model selects.

        When the selected provider declares REQUIRES_API_KEY = True, an API key
        input row is shown inline with Save and Cancel buttons.  The key is
        persisted to ~/.config/codingagent/prefs.json only — never to
        providers.json.
        """

        BINDINGS = [("escape", "close_modal", "Close")]

        DEFAULT_CSS = """
        SettingsModal {
            align: center middle;
            background: rgba(0, 0, 0, 0.55);
        }
        #settings_dialog {
            width: 66;
            height: auto;
            background: #1e1e1e;
            border: solid #007acc;
            padding: 1 2;
        }
        #settings_title {
            text-align: center;
            text-style: bold;
            color: #9cdcfe;
            margin-bottom: 1;
        }
        .s_rule {
            color: #3c3c3c;
            height: 1;
        }
        .s_section {
            color: #569cd6;
            text-style: bold;
            margin-top: 1;
            height: 1;
        }
        #session_row {
            height: 3;
            margin-top: 1;
        }
        #btn_new_session {
            width: 1fr;
            margin-right: 1;
        }
        #btn_compact {
            width: 1fr;
        }
        .s_row {
            height: 3;
            margin-top: 1;
        }
        .s_label {
            width: 10;
            content-align: left middle;
            color: #9cdcfe;
        }
        .s_select {
            width: 1fr;
        }
        #apikey_section {
            margin-top: 1;
        }
        #apikey_row {
            height: 3;
            margin-top: 1;
        }
        #key_input {
            width: 1fr;
            margin-right: 1;
        }
        #btn_save_key {
            width: auto;
            min-width: 8;
            margin-right: 1;
        }
        #btn_cancel_key {
            width: auto;
            min-width: 4;
        }
        #apikey_hint {
            color: #6a9955;
            height: 1;
            margin-top: 0;
        }
        #s_info {
            color: #6a9955;
            margin-top: 1;
            height: 1;
        }
        #footer_row {
            align: right middle;
            height: 3;
            margin-top: 1;
        }
        #btn_close {
            width: auto;
            min-width: 9;
        }
        """

        def __init__(self, controller=None):
            super().__init__()
            self.controller = controller
            self.providers: List[str] = []
            self.current_provider: Optional[str] = None

        def _needs_api_key(self, provider: Optional[str]) -> bool:
            """Return True if *provider* requires an API key."""
            if not provider or not self.controller:
                return False
            try:
                return self.controller.provider_requires_api_key(provider)
            except Exception:
                return False

        def compose(self):
            # Gather live state before composing
            cur_model: Optional[str] = None
            models: List[str] = []
            msg_count = 0
            wd_str = "."
            try:
                cur_model = getattr(self.app, "_current_model", None)
            except Exception:
                pass
            try:
                if self.controller and self.current_provider:
                    models = (
                        self.controller.get_cached_models(self.current_provider) or []
                    )
            except Exception:
                pass
            try:
                orch = getattr(self.app, "orchestrator", None)
                if orch:
                    msgs = getattr(getattr(orch, "msg_mgr", None), "messages", [])
                    msg_count = len(msgs)
                    wd = str(getattr(orch, "working_dir", "."))
                    wd_str = wd if len(wd) <= 34 else "…" + wd[-33:]
            except Exception:
                pass

            prov_opts = (
                [(p, p) for p in self.providers] if self.providers else [("—", "")]
            )
            prov_val = (
                self.current_provider
                if self.current_provider in self.providers
                else (self.providers[0] if self.providers else None)
            )
            model_opts = [(m, m) for m in models] if models else [("—", "")]
            model_val = (
                cur_model if cur_model in models else (models[0] if models else None)
            )

            with Container(id="settings_dialog"):
                yield Label("⚙  Settings", id="settings_title")
                yield Static("─" * 60, classes="s_rule")

                yield Label("Session", classes="s_section")
                with Horizontal(id="session_row"):
                    yield Button("New Session", id="btn_new_session")
                    yield Button("Compact", id="btn_compact")

                yield Label("Provider", classes="s_section")
                with Horizontal(classes="s_row"):
                    yield Label("Provider", classes="s_label")
                    yield Select(
                        options=prov_opts,
                        value=prov_val,
                        id="sel_provider",
                        classes="s_select",
                    )
                with Horizontal(classes="s_row"):
                    yield Label("Model", classes="s_label")
                    yield Select(
                        options=model_opts,
                        value=model_val,
                        id="sel_model",
                        classes="s_select",
                    )

                # API key section — shown/hidden based on provider selection
                with Container(
                    id="apikey_section",
                    # display toggled at runtime via _update_apikey_visibility
                ):
                    yield Label("API Key", classes="s_section")
                    with Horizontal(id="apikey_row"):
                        yield Input(
                            placeholder="paste key here…",
                            password=True,
                            id="key_input",
                        )
                        yield Button("Save", id="btn_save_key")
                        yield Button("✕", id="btn_cancel_key")
                    yield Static(
                        "  stored in ~/.config/codingagent/prefs.json",
                        id="apikey_hint",
                    )

                yield Static("─" * 60, classes="s_rule")
                yield Static(f"  msgs: {msg_count}   dir: {wd_str}", id="s_info")
                with Horizontal(id="footer_row"):
                    yield Button("Close", id="btn_close")

        def on_mount(self) -> None:
            """Set initial API key section visibility."""
            self._update_apikey_visibility(self.current_provider)

        def _update_apikey_visibility(self, provider: Optional[str]) -> None:
            """Show or hide the API key section based on whether provider needs a key."""
            try:
                section = self.query_one("#apikey_section")
                section.display = self._needs_api_key(provider)
            except Exception:
                pass

        def action_close_modal(self) -> None:
            self.app.pop_screen()

        def on_select_changed(self, event) -> None:
            sel_id = getattr(getattr(event, "select", None), "id", None)
            val = getattr(event, "value", None)
            if not val or val == "—":
                return
            if sel_id == "sel_provider":
                if hasattr(self.app, "_set_provider"):
                    self.app._set_provider(val)  # type: ignore[reportAttributeAccessIssue]
                # Show/hide API key row for the new provider
                self._update_apikey_visibility(val)
                # Clear any previously entered key
                try:
                    self.query_one("#key_input", Input).value = ""
                except Exception:
                    pass
                # Reload model list for the new provider
                if self.controller:
                    try:
                        new_models = self.controller.get_cached_models(val) or []
                        if not new_models:
                            new_models = (
                                self.controller.fetch_models_from_provider_sync(val)
                                or []
                            )
                        if new_models:
                            try:
                                sel = self.query_one("#sel_model", Select)
                                sel.set_options([(m, m) for m in new_models])
                            except Exception:
                                pass
                    except Exception:
                        pass
            elif sel_id == "sel_model":
                if hasattr(self.app, "_set_model"):
                    self.app._set_model(val)  # type: ignore[reportAttributeAccessIssue]

        def on_button_pressed(self, event: Button.Pressed) -> None:
            id_ = event.button.id
            if id_ == "btn_close":
                self.app.pop_screen()
            elif id_ == "btn_new_session":
                self.app.pop_screen()
                _do_new_session(self.app, self.controller)
            elif id_ == "btn_compact":
                self.app.pop_screen()
                _do_compact_session(self.app)
            elif id_ == "btn_save_key":
                self._save_api_key()
            elif id_ == "btn_cancel_key":
                try:
                    self.query_one("#key_input", Input).value = ""
                except Exception:
                    pass

        def _save_api_key(self) -> None:
            """Read key_input and persist via controller.save_api_key()."""
            try:
                key_val = self.query_one("#key_input", Input).value.strip()
            except Exception:
                key_val = ""
            if not key_val:
                try:
                    self.app._safe_write(  # type: ignore[reportAttributeAccessIssue]
                        "[yellow]API key is empty — not saved.[/yellow]"
                    )
                except Exception:
                    pass
                return
            provider = self.current_provider
            # update current_provider from live select if possible
            try:
                provider = self.query_one("#sel_provider", Select).value or provider
            except Exception:
                pass
            if not provider or provider == "—":
                return
            saved = False
            if self.controller:
                try:
                    saved = self.controller.save_api_key(provider, key_val)
                except Exception:
                    pass
            if saved:
                try:
                    self.query_one("#key_input", Input).value = ""
                except Exception:
                    pass
                try:
                    self.app._safe_write(f"[dim]API key saved for {provider}.[/dim]")  # type: ignore[reportAttributeAccessIssue]
                except Exception:
                    pass
            else:
                try:
                    self.app._safe_write(  # type: ignore[reportAttributeAccessIssue]
                        f"[red]Failed to save API key for {provider}.[/red]"
                    )
                except Exception:
                    pass

        def open_with(
            self, providers: List[str], current_provider: Optional[str] = None
        ) -> None:
            self.providers = providers
            self.current_provider = current_provider

    # Inject settings modal into CodingAgentTextualApp
    _orig_compose = CodingAgentTextualApp.compose

    def _compose_with_settings(self) -> ComposeResult:
        for item in _orig_compose(self):
            yield item
        self._settings_modal = SettingsModal(controller=SettingsPanelController())

    CodingAgentTextualApp.compose = _compose_with_settings

    # Provide a factory function for creating the app instance when Textual is present
    def create_app(orchestrator: Optional[Orchestrator] = None):
        return CodingAgentTextualApp(orchestrator=orchestrator)


# End of module

# Compatibility helper for tests: ensure TextualAppImpl._render_message_safe exists
try:
    from rich.markup import escape as _rich_escape
except Exception:
    _rich_escape = None


def _strip_markup(text: str) -> str:
    # simple fallback to remove bracket-style tags like [bold]...[/bold]
    import re

    if not isinstance(text, str):
        return str(text)
    # remove tags like [bold], [dim], [/bold], [/dim], [color], [/color]
    cleaned = re.sub(r"\[/?[a-zA-Z0-9_\-#=;\s]*\]", "", text)
    return cleaned


# If the module provides a TextualAppImpl class, add a compatibility method to it
try:
    TextualAppImpl  # type: ignore  # noqa: F821
except Exception:
    # Define a minimal shim if class not present (for test compatibility)
    class TextualAppImpl:
        def __init__(self):
            pass

        def _render_message_safe(self, text: str) -> str:
            # Strip markup tags to produce clean plain text
            return _strip_markup(text)
