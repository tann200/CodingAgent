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
    try:
        from textual.app import App, ComposeResult  # type: ignore
        from textual.containers import Container, Horizontal  # type: ignore
        from textual.widgets import Header, Footer, Static, Input, RichLog, Button  # type: ignore
        from textual.events import Key, Paste  # type: ignore
        from textual.screen import ModalScreen  # type: ignore
        from textual.widgets import Select, Label  # type: ignore
    except Exception:
        pass


# Try to dynamically import Textual so static analyzers won't error when it's absent.
TEXTUAL_AVAILABLE: bool = False

App: Any = object  # fallback
Container: Any = None
Horizontal: Any = None
Header: Any = None
Footer: Any = None
Static: Any = None
Input: Any = None
RichLog: Any = None
Button: Any = None
ComposeResult: Any = None
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
    TEXTUAL_AVAILABLE = True
except Exception:
    # Textual isn't available at runtime; fall back to plain-mode.
    TEXTUAL_AVAILABLE = False


class TextualAppBase:
    """Base behavior used by both real Textual app and stub."""

    def __init__(self, orchestrator: Optional[Orchestrator] = None):
        self.orchestrator = orchestrator or Orchestrator()
        # internal chat history as tuple(role, text)
        self.history: List[tuple] = []
        self._history_lock = threading.Lock()  # H4: protects concurrent history access
        # event bus
        try:
            self.event_bus = get_event_bus()
            if self.event_bus:
                self.event_bus.subscribe("ui.notification", self._on_ui_notification)
        except Exception:
            self.event_bus = None
        # cancel event for interrupting agent
        self._cancel_event = threading.Event()

    def send_prompt(self, prompt: str) -> None:
        """Send a prompt to the orchestrator in a background thread."""
        with self._history_lock:
            self.history.append(("user", prompt))
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
                self.on_agent_result(f"[ERROR] {e}")
            except Exception:
                pass

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
            if not getattr(self, "output", None):
                return
            # Try rich.text if available
            try:
                from rich.text import Text  # type: ignore

                try:
                    self.output.write(Text.from_markup(msg))
                    return
                except Exception:
                    # fall through to plain write
                    pass
            except Exception:
                pass
            # Plain write fallback
            try:
                self.output.write(msg)
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

    def create_app(orchestrator: Optional[Orchestrator] = None):
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

    # M3: Known slash commands for Tab autocomplete
    SLASH_COMMANDS = [
        "/help",
        "/clear",
        "/continue",
        "/interrupt",
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
                            from src.core.inference.provider_context import get_context_budget
                            ctx_limit = get_context_budget()
                            ctx_limit_str = f"{ctx_limit // 1000}k" if ctx_limit >= 1000 else str(ctx_limit)
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
                        self.tool_activity_label = Static(
                            "—", id="tool_activity_info"
                        )
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
                        # C2: Dashboard events
                        ("plan.progress", self._on_plan_progress_ui),
                        ("tool.execute.finish", self._on_tool_finish_ui),
                        ("tool.execute.error", self._on_tool_error_ui),
                        # M4: Diff preview before file edits
                        ("file.diff.preview", self._on_diff_preview_ui),
                    ]
                    for event_name, cb in _subs:
                        eb.subscribe(event_name, cb)
                        self._eb_subscriptions.append((event_name, cb))
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
                                pm.get_models_from_api(prov)
                                # trigger UI refresh on bus
                                self.event_bus.publish(
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
                        Text.from_markup(f"[bold blue]User:[/bold blue] {raw_text}")
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
            self._agent_running = True
            self._agent_thread = threading.Thread(
                target=self.send_prompt, args=(text,), daemon=True
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
            self._agent_running = False
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
            import re

            diff_pattern = re.compile(r"```diff\n(.*?)\n```", re.DOTALL)
            diff_matches = list(diff_pattern.finditer(content))

            if diff_matches:
                last_end = 0
                for match in diff_matches:
                    before_diff = content[last_end : match.start()]
                    if before_diff.strip():
                        try:
                            from rich.text import Text

                            self.output.write(
                                Text.from_markup(
                                    f"[bold]Assistant:[/bold] {before_diff}"
                                )
                            )
                        except Exception:
                            self._safe_write(f"Assistant: {before_diff}")

                    self._render_side_by_side_diff(match.group(1))
                    last_end = match.end()

                remaining = content[last_end:]
                if remaining.strip():
                    try:
                        from rich.text import Text

                        self.output.write(Text.from_markup(remaining))
                    except Exception:
                        self._safe_write(remaining)
                return

            thinking_pattern = re.compile(r"<think>(.*?)</think>", re.DOTALL)
            thinking_match = thinking_pattern.search(content)

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
            """Render a unified diff as a side-by-side view using Rich Tables."""
            import re

            try:
                from rich.table import Table
                from rich.text import Text

                lines = diff_content.strip().split("\n")
                left_lines = []
                right_lines = []
                current_left = []
                current_right = []

                for line in lines:
                    if line.startswith("---"):
                        continue
                    if line.startswith("+++"):
                        continue
                    if line.startswith("@@"):
                        if current_left or current_right:
                            left_lines.append(current_left)
                            right_lines.append(current_right)
                        current_left = []
                        current_right = []
                        match = re.search(r"@@ -(\d+),?\d* \+(\d+),?\d* @@", line)
                        if match:
                            current_left.append(f"@@ -{match.group(1)}")
                            current_right.append(f"@@ +{match.group(2)}")
                    elif line.startswith("-"):
                        current_left.append(line[1:] if len(line) > 1 else "")
                    elif line.startswith("+"):
                        current_right.append(line[1:] if len(line) > 1 else "")
                    elif line.startswith(" "):
                        current_left.append(line[1:] if len(line) > 1 else "")
                        current_right.append(line[1:] if len(line) > 1 else "")
                    else:
                        current_left.append(line)

                if current_left or current_right:
                    left_lines.append(current_left)
                    right_lines.append(current_right)

                table = Table(show_header=True, show_lines=True, expand=True)
                table.add_column("Before", style="red", ratio=1)
                table.add_column("After", style="green", ratio=1)

                for block_left, block_right in zip(left_lines, right_lines):
                    for line in block_left:
                        if line.startswith("@@"):
                            table.add_row(Text(line, style="cyan bold"), "")
                        elif line:
                            table.add_row(Text(line), "")
                        else:
                            table.add_row("", "")
                    for line in block_right:
                        if line.startswith("@@"):
                            table.add_row("", Text(line, style="cyan bold"))
                        elif line:
                            table.add_row("", Text(line))

                self.output.write(table)
            except Exception:
                self._safe_write(diff_content)

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
                task_state_path.write_text(
                    "# Current Task\n\n# Completed Steps\n\n# Next Step\n"
                )
                guilogger.info("Task state cleared for new session")
                if hasattr(self, "task_state_label"):
                    self.task_state_label.update("No active task")
                if hasattr(self, "context_label") and self.context_label:
                    self.context_label.update(
                        "Model: --\nUsed: 0\nPrompt: 0\nReply: 0\nLatency: 0.00s"
                    )
            except Exception as e:
                guilogger.error(f"Failed to clear task state on new session: {e}")

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

        # C2: Dashboard event handlers — update sidebar plan progress and tool activity labels

        def _on_plan_progress_ui(self, payload: Dict[str, Any]) -> None:
            """Update the plan progress sidebar label from plan.progress events."""
            try:
                if not self.plan_progress_label or not isinstance(payload, dict):
                    return
                step = payload.get("step", 0)
                total = payload.get("total", 0)
                desc = payload.get("description", "")
                if total:
                    bar = "█" * step + "░" * (total - step)
                    text = f"Step {step}/{total}\n{bar}\n{desc[:30]}" if desc else f"Step {step}/{total}\n{bar}"
                else:
                    text = desc[:40] if desc else "Running…"
                self._schedule_callback(self.plan_progress_label.update, text)
            except Exception:
                pass

        def _on_tool_finish_ui(self, payload: Dict[str, Any]) -> None:
            """Update the tool activity sidebar label on tool completion."""
            try:
                if not self.tool_activity_label or not isinstance(payload, dict):
                    return
                tool = payload.get("tool", "?")
                ok = payload.get("ok", True)
                status = "✓" if ok else "✗"
                self._schedule_callback(
                    self.tool_activity_label.update, f"{status} {tool}"
                )
            except Exception:
                pass

        def _on_tool_error_ui(self, payload: Dict[str, Any]) -> None:
            """Update the tool activity sidebar label on tool error."""
            try:
                if not self.tool_activity_label or not isinstance(payload, dict):
                    return
                tool = payload.get("tool", "?")
                self._schedule_callback(
                    self.tool_activity_label.update, f"✗ {tool} (error)"
                )
            except Exception:
                pass

        def _on_diff_preview_ui(self, payload: Dict[str, Any]) -> None:
            """M4: Show diff preview in the output log before/after a file write.

            Receives file.diff.preview events published by write_file / edit_file /
            edit_by_line_range in file_tools.py.
            """
            try:
                if not self.output or not isinstance(payload, dict):
                    return
                path = payload.get("path", "?")
                diff = payload.get("diff", "")
                is_new = payload.get("is_new_file", False)
                if not diff:
                    return
                label = "🆕 New file" if is_new else "✏️  Edit"
                header = f"[bold cyan]{label}:[/bold cyan] [dim]{path}[/dim]"

                # Colour the diff lines: + green, - red, @@ cyan, rest dim
                coloured_lines = []
                for line in diff.splitlines()[:60]:  # cap at 60 lines for readability
                    if line.startswith("+") and not line.startswith("+++"):
                        coloured_lines.append(f"[green]{line}[/green]")
                    elif line.startswith("-") and not line.startswith("---"):
                        coloured_lines.append(f"[red]{line}[/red]")
                    elif line.startswith("@@"):
                        coloured_lines.append(f"[cyan]{line}[/cyan]")
                    else:
                        coloured_lines.append(f"[dim]{line}[/dim]")

                total_lines = len(diff.splitlines())
                if total_lines > 60:
                    coloured_lines.append(f"[dim]… {total_lines - 60} more lines[/dim]")

                diff_block = "\n".join(coloured_lines)
                text = f"{header}\n{diff_block}"
                self._schedule_callback(self.output.write, text)
            except Exception:
                pass

        def action_toggle_log(self) -> None:
            if self.sys_log:
                self.sys_log.display = not self.sys_log.display

        def action_open_settings(self) -> None:
            # U4: Guard against _settings_modal not yet initialised (compose() not yet run)
            if not getattr(self, "_settings_modal", None):
                guilogger.warning("action_open_settings: settings modal not yet initialised")
                return
            try:
                providers = []
                try:
                    pm = get_provider_manager()
                    providers = list(pm.list_providers())
                except Exception:
                    providers = []
                self._settings_modal.open_with(
                    providers, providers[0] if providers else None
                )
                self.push_screen(self._settings_modal)
            except Exception as e:
                guilogger.error(f"Failed to open settings: {e}")

        def action_interrupt_agent(self) -> None:
            """Interrupt the currently running agent."""
            # Check if agent is running - more robust check
            if self._agent_running:
                thread_alive = self._agent_thread and self._agent_thread.is_alive()
                if thread_alive:
                    guilogger.info("User interrupted agent (Escape pressed)")
                    self._cancel_event.set()
                    self.output.write(
                        "[yellow]⚠ Agent interrupted. Type 'continue' to resume or enter a new command.[/yellow]"
                    )
                    self._agent_running = False
                    return
            guilogger.info("No agent running to interrupt")

        def action_force_interrupt_agent(self) -> None:
            """Force interrupt the agent immediately (double-ESC)."""
            guilogger.info("User force interrupted agent (double-escape pressed)")
            self._cancel_event.set()
            if self._agent_running:
                self.output.write(
                    "[red]⚠ Agent force stopped. Type 'continue' to resume or enter a new command.[/red]"
                )
                self._agent_running = False
            if self._agent_thread and self._agent_thread.is_alive():
                guilogger.info("Agent thread still running, setting cancel event")

        def _save_state_for_continue(self) -> None:
            """Save current agent state for continue functionality."""
            try:
                if self.orchestrator and hasattr(self.orchestrator, "msg_mgr"):
                    msg_mgr = self.orchestrator.msg_mgr
                    # U6: persist full AgentState fields so continue can resume mid-plan
                    agent_state = getattr(self.orchestrator, "_last_agent_state", {}) or {}
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
                if self.orchestrator and hasattr(self.orchestrator, "msg_mgr"):
                    msg_mgr = self.orchestrator.msg_mgr
                    if hasattr(msg_mgr, "messages") and self._continue_state.get(
                        "history"
                    ):
                        msg_mgr.messages = self._continue_state["history"].copy()
                    if hasattr(self.orchestrator, "_session_read_files"):
                        self.orchestrator._session_read_files = set(
                            self._continue_state.get("session_read_files", [])
                        )
                    # U6: restore full AgentState fields for mid-plan resume
                    if not hasattr(self.orchestrator, "_last_agent_state"):
                        self.orchestrator._last_agent_state = {}
                    for key in ("current_plan", "current_step", "working_dir", "step_retry_counts"):
                        val = self._continue_state.get(key)
                        if val is not None:
                            self.orchestrator._last_agent_state[key] = val
                    guilogger.info("State restored for continue")
                    return True
            except Exception as e:
                guilogger.error(f"Failed to restore state: {e}")
            return False

    # Add Settings modal support when Textual is available - import dynamically
    try:
        _screen = importlib.import_module("textual.screen")
        ModalScreen = getattr(_screen, "ModalScreen")
        _widgets2 = importlib.import_module("textual.widgets")
        Select = getattr(_widgets2, "Select")
        Label = getattr(_widgets2, "Label")
    except Exception:
        ModalScreen = object
        Select = object
        Label = object

    class ConnectProviderModal(ModalScreen):
        DEFAULT_CSS = """
        ConnectProviderModal {
            align: center middle;
            background: rgba(0, 0, 0, 0.7);
        }
        #provider_dialog {
            width: 50;
            height: auto;
            background: #252526;
            border: solid #007acc;
            padding: 1 2;
        }
        .provider_btn { width: 100%; margin-top: 1; }
        """

        def __init__(self, providers: List[str], current: Optional[str] = None):
            super().__init__()
            self.providers = providers
            self.current = current
            self.provider_select: Optional[Select] = None

        def compose(self):
            with Container(id="provider_dialog"):
                yield Label("🔌 Connect Provider", classes="settings_title")
                opts = [(p, p) for p in self.providers]
                self.provider_select = Select(
                    options=opts, value=self.current, id="provider_select"
                )
                yield self.provider_select
                yield Button(
                    "Connect",
                    id="connect_btn",
                    classes="provider_btn",
                    variant="primary",
                )
                yield Button("Cancel", id="cancel_btn", classes="provider_btn")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_btn":
                self.app.pop_screen()
            elif event.button.id == "connect_btn":
                val = self.provider_select.value
                if val:
                    # Update UI and provider
                    if hasattr(self.app, "_set_provider"):
                        self.app._set_provider(val)
                self.app.pop_screen()

    class SelectModelModal(ModalScreen):
        DEFAULT_CSS = """
        SelectModelModal {
            align: center middle;
            background: rgba(0, 0, 0, 0.7);
        }
        #model_dialog {
            width: 50;
            height: auto;
            background: #252526;
            border: solid #007acc;
            padding: 1 2;
        }
        .model_btn { width: 100%; margin-top: 1; }
        """

        def __init__(self, models: List[str], current: Optional[str] = None):
            super().__init__()
            self.models = models
            self.current = current
            self.model_select: Optional[Select] = None

        def compose(self):
            with Container(id="model_dialog"):
                yield Label("🧠 Select Model", classes="settings_title")
                opts = [(m, m) for m in self.models]
                val = (
                    self.current
                    if self.current in self.models
                    else (self.models[0] if self.models else None)
                )
                self.model_select = Select(options=opts, value=val, id="model_select")
                yield self.model_select
                yield Button(
                    "Select", id="select_btn", classes="model_btn", variant="primary"
                )
                yield Button("Cancel", id="cancel_btn", classes="model_btn")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_btn":
                self.app.pop_screen()
            elif event.button.id == "select_btn":
                val = self.model_select.value
                if val:
                    if hasattr(self.app, "_set_model"):
                        self.app._set_model(val)
                self.app.pop_screen()

    class SettingsModal(ModalScreen):
        DEFAULT_CSS = """
        SettingsModal {
            align: center middle;
            background: rgba(0, 0, 0, 0.7);
        }
        #settings_dialog {
            width: 50;
            height: auto;
            background: #252526;
            border: solid #007acc;
            padding: 1 2;
        }
        .settings_title {
            text-align: center;
            text-style: bold;
            margin-bottom: 1;
        }
        .settings_btn {
            width: 100%;
            margin-top: 1;
        }
        """

        def __init__(self, controller=None):
            super().__init__()
            self.controller = controller or None
            self.providers = []
            self.current_provider = None

        def compose(self):
            with Container(id="settings_dialog"):
                yield Label("⚙️  Settings", classes="settings_title")
                yield Button(
                    "New Session", id="settings_new_session", classes="settings_btn"
                )
                yield Button(
                    "Compact Session",
                    id="settings_compact_session",
                    classes="settings_btn",
                )
                yield Button(
                    "Connect Provider",
                    id="settings_connect_provider",
                    classes="settings_btn",
                )
                yield Button(
                    "Select Model", id="settings_select_model", classes="settings_btn"
                )
                yield Button(
                    "Close",
                    id="settings_close",
                    classes="settings_btn",
                    variant="error",
                )

        def on_button_pressed(self, event: Button.Pressed) -> None:
            id_ = event.button.id
            if id_ == "settings_close":
                self.app.pop_screen()
            elif id_ == "settings_new_session":
                if self.controller:
                    self.controller.start_new_session()
                # clear history
                if hasattr(self.app, "history"):
                    self.app.history.clear()
                if hasattr(self.app, "output") and self.app.output:
                    self.app.output.clear()
                    self.app.output.write("[System] Started new session.")
                self.app.pop_screen()
            elif id_ == "settings_compact_session":
                if hasattr(self.app, "output") and self.app.output:
                    self.app.output.write(
                        "[System] Compacting session... (Placeholder)"
                    )
                self.app.pop_screen()
            elif id_ == "settings_connect_provider":
                self.app.pop_screen()
                self.app.push_screen(
                    ConnectProviderModal(
                        self.providers, getattr(self.app, "_current_provider", None)
                    )
                )
            elif id_ == "settings_select_model":
                self.app.pop_screen()
                current_prov = getattr(self.app, "_current_provider", None)
                models = []
                try:
                    if self.controller and current_prov:
                        models = self.controller.get_cached_models(current_prov)
                except Exception:
                    pass
                self.app.push_screen(
                    SelectModelModal(models, getattr(self.app, "_current_model", None))
                )

        def open_with(
            self, providers: List[str], current_provider: Optional[str] = None
        ):
            self.providers = providers
            self.current_provider = current_provider

    # Inject settings modal into CodingAgentTextualApp
    _orig_compose = CodingAgentTextualApp.compose

    def _compose_with_settings(self) -> ComposeResult:
        for item in _orig_compose(self):
            yield item
        # modal is created and mounted lazily
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
