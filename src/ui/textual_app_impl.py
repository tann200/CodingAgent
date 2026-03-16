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
from typing import List, Optional

from src.core.orchestration.event_bus import get_event_bus
from src.core.llm_manager import get_provider_manager
from src.core.orchestration.orchestrator import Orchestrator
from src.core.logger import logger as guilogger
from src.ui.views.settings_panel import SettingsPanelController


# Try to import Textual. If not available, provide a stub that warns the user.
try:
    from textual.app import App, ComposeResult
    from textual.containers import Container, Horizontal
    from textual.widgets import Header, Footer, Static, Input, RichLog, Button

    TEXTUAL_AVAILABLE = True
    # Ensure Textual's call_from_thread behaves safely in our test/runtime environment
    try:
        import asyncio as _asyncio

        def _safe_textual_call_from_thread(self, callback, *args, **kwargs):
            try:
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(lambda: callback(*args, **kwargs))
                else:
                    callback(*args, **kwargs)
            except Exception:
                try:
                    callback(*args, **kwargs)
                except Exception:
                    pass

        # Patch the App class method so textual internals won't return un-awaited coroutines
        try:
            App.call_from_thread = _safe_textual_call_from_thread
        except Exception:
            pass
    except Exception:
        pass
except Exception:
    TEXTUAL_AVAILABLE = False
    App = object  # type: ignore


class TextualAppBase:
    """Base behavior used by both real Textual app and stub."""

    def __init__(self, orchestrator: Optional[Orchestrator] = None):
        self.orchestrator = orchestrator or Orchestrator()
        # internal chat history as tuple(role, text)
        self.history: List[tuple] = []
        # event bus
        try:
            self.event_bus = get_event_bus()
        except Exception:
            self.event_bus = None

    def send_prompt(self, prompt: str) -> None:
        """Send a prompt to the orchestrator in a background thread."""
        self.history.append(("user", prompt))
        t = threading.Thread(target=self._run_agent, args=(prompt,), daemon=True)
        t.start()

    def _run_agent(self, prompt: str) -> None:
        try:
            # Build messages list (system prompt is injected by orchestrator)
            messages = [{"role": "user", "content": prompt}]
            guilogger.info(f"TextualApp: sending prompt to orchestrator: {prompt[:60]}")
            res = self.orchestrator.run_agent_once(None, messages, {})
            # res expected to have 'assistant_message' or 'raw'
            assistant_msg = None
            try:
                if isinstance(res, dict) and res.get("assistant_message"):
                    assistant_msg = res.get("assistant_message")
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
            # append
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
    from textual.events import Key
    from textual.events import Paste

    class ChatInput(Input):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.history = []
            self.history_index = -1

        def on_key(self, event: Key) -> None:
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
        ]

        def __init__(self, orchestrator: Optional[Orchestrator] = None):
            App.__init__(self)
            TextualAppBase.__init__(self, orchestrator=orchestrator)
            self.output: Optional[RichLog] = None
            self.sys_log: Optional[RichLog] = None
            self.mode_label: Optional[Static] = None
            self.provider_model_label: Optional[Static] = None
            self.context_label: Optional[Static] = None
            self.input_widget: Optional[Input] = None

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
                                "⌨️  ^O Settings | ^L Toggle Log | Enter Send",
                                id="legend_info",
                            )

                with Container(id="sidebar"):
                    with Container(id="sidebar_top"):
                        yield Static("📊 Context", classes="sidebar_title")
                        self.context_label = Static(
                            "Used: 0\nLimit: 128k\n0%", id="context_info"
                        )
                        yield self.context_label

                        yield Static("📝 Task State", classes="sidebar_title")
                        self.task_state_label = Static(
                            "No active task", id="task_state_info"
                        )
                        yield self.task_state_label

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
                    from src.core.llm_manager import (
                        _ensure_provider_manager_initialized_sync,
                    )

                    _ensure_provider_manager_initialized_sync()

                    if getattr(pm, "_event_bus", None) is None:
                        pm.set_event_bus(get_event_bus())
            except Exception:
                pass

            # subscribe to events
            try:
                eb = get_event_bus()
                if eb:
                    eb.subscribe(
                        "provider.status.changed", self._on_provider_status_changed
                    )
                    eb.subscribe("provider.models.list", self._on_provider_models)
                    eb.subscribe("log.new", self._on_log_new)
                    eb.subscribe("model.response", self._on_token_usage)
                    eb.subscribe("model.routing", self._on_model_routing)
                    eb.subscribe("session.new", self._on_session_new)
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

            # display in output immediately
            if self.output:
                self.output.write(f"User: {text}")
            # send to orchestrator in background
            threading.Thread(target=self.send_prompt, args=(text,), daemon=True).start()
            # clear input
            if self.input_widget:
                self.input_widget.value = ""

        def on_agent_result(self, content: str) -> None:
            # called from background thread via call_from_thread
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

            processed_content = content
            if "<think>" in content and "</think>" in content:
                match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                if match:
                    thinking = match.group(1).strip()
                    rest = content.replace(match.group(0), "").strip()
                    self.output.write("[dim italic]Thinking:[/dim italic]")
                    self.output.write(f"[dim]{thinking}[/dim]")
                    self.output.write("")
                    processed_content = rest

            if processed_content:
                self.output.write(f"[bold]Assistant:[/bold] {processed_content}")

            if hasattr(self, "task_state_label"):
                self._refresh_task_state()

        def _refresh_task_state(self):
            try:
                import os

                wd = getattr(self.orchestrator, "working_dir", None) or os.getcwd()
                from pathlib import Path

                task_state_path = Path(wd) / ".agent-context" / "TASK_STATE.md"
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
                    if task_info:
                        guilogger.info(f"Task State label updating to: {task_info}")
                        self.task_state_label.update(task_info)
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

        def action_toggle_log(self) -> None:
            if self.sys_log:
                self.sys_log.display = not self.sys_log.display

        def action_open_settings(self) -> None:
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

    # Add Settings modal support when Textual is available
    from textual.screen import ModalScreen
    from textual.widgets import Select, Label

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
