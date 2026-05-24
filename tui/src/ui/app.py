"""
AgentApp — TUI System Specification v2.0 compliant Textual application.
Wires exclusively through AgentBridge → EventBus; never imports src.core directly.
"""

from __future__ import annotations

import sys as _sys
import threading
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .components import FilePickerOverlay, StreamView
    from .components import SubagentProgress

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual import on
from textual.widgets import Button, Header, Label, Static

from .bus import (
    DeviceFlowCompleteEvent,
    DeviceFlowErrorEvent,
    OrchestratorReadyEvent,
    ProviderStatusChangeEvent,
    SystemSettingsLoaded,
)
from .components import ChatTextArea
from .components.status_bar import StatusBarMixin, ROLE_LABELS as _SB_ROLE_LABELS, ROLE_COLORS as _SB_ROLE_COLORS
from .components.chat_mixin import ChatDisplayMixin
from ._app_session_mixin import AppSessionMixin
from ._app_tool_handlers_mixin import AppToolHandlersMixin
from ._app_message_handlers_mixin import AppMessageHandlersMixin
from ._app_slash_commands_mixin import AppSlashCommandsMixin
from ._app_status_handlers_mixin import AppStatusHandlersMixin
from .events import (
    ConnectProvider,
    PaletteCommand,
    RequestSystemSettings,
    SaveProviderCredentials,
    SlashCommand,
    StartGithubDeviceFlow,
    UpdateRoleModel,
    UpdateSettings,
)
from .logging import get_logger
from .settings import SettingsStore

logger = get_logger("app")

ROLE_LABELS = _SB_ROLE_LABELS
ROLE_COLORS = _SB_ROLE_COLORS


def _load_llm_manager_module():
    """Return the real src.core.inference.llm_manager module, cached in sys.modules."""
    import importlib.util

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
        spec.loader.exec_module(mod)
    except Exception:
        _sys.modules.pop(_MOD_NAME, None)
        raise
    return mod


def _load_copilot_auth_module():
    """Return the real src.core.inference.adapters.github_copilot_auth module, cached."""
    import importlib.util

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
        spec.loader.exec_module(mod)
    except Exception:
        _sys.modules.pop(_MOD_NAME, None)
        raise
    return mod


class AgentApp(
    App[None],
    StatusBarMixin,
    ChatDisplayMixin,
    AppSessionMixin,
    AppToolHandlersMixin,
    AppMessageHandlersMixin,
    AppSlashCommandsMixin,
    AppStatusHandlersMixin,
):
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
    agent_running = reactive(False)

    def __init__(self) -> None:
        super().__init__()
        import uuid as _uuid

        from .core_bridge import AgentBridge

        _init_wd: Optional[Path] = getattr(self, "_initial_working_dir", None)
        self._bridge = AgentBridge(self, working_dir=_init_wd)
        self._session_id: str = str(_uuid.uuid4())
        self._current_stream: Optional[StreamView] = None
        self._role_cycle = ["lead_architect", "full_stack_engineer", "qa_lead"]
        self._role_idx = 0
        self._modified_files: list[str] = []
        self._settings = SettingsStore()
        self._last_esc_time: float = 0.0
        self._tool_widgets: dict[str, Static] = {}
        self._tool_args: dict[str, dict] = {}
        self._subagent_widgets: dict[str, SubagentProgress] = {}
        self._continue_state: Optional[dict] = None
        self._last_task_text: str = ""
        self._pending_perm_count: int = 0
        self._allow_always_tools: set[str] = set()
        import collections as _collections

        self._queued_messages: _collections.deque[str] = _collections.deque()
        self._queued_widget: Optional[Static] = None
        self._queued_message: Optional[str] = None
        self._tool_call_count: int = 0
        self._session_input_tokens: int = 0
        self._session_output_tokens: int = 0
        self._at_picker_active: bool = False
        self._at_picker_matches: list[str] = []
        self._at_picker_index: int = 0
        self._at_picker_widget: Optional[FilePickerOverlay] = None
        self._at_prefix: str = ""
        self._at_file_cache: list[str] = []
        self._at_file_cache_ts: float = 0.0
        self._palette_active: bool = False
        self._palette_matches: list[str] = []
        self._oauth_screen: Optional[object] = None
        self._device_flow_cancel: threading.Event = threading.Event()
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
                yield FilePickerOverlay(id="file_picker")
                yield ChatTextArea(
                    id="user_input",
                    placeholder="Type a message or /help … (Esc to interrupt)",
                )
            with VerticalScroll(id="right_sidebar"):
                yield Label("TASK", classes="sb_title")
                yield Static("idle", id="sb_task_status")
                yield Label("PLAN PROGRESS", classes="sb_title")
                yield Static("—", id="sb_plan_bar")
                yield Static("", id="sb_plan_desc")
                yield Label("LAST TOOL", classes="sb_title")
                yield Static("—", id="sb_tool_activity")
                yield Label("SUBAGENTS", classes="sb_title")
                yield Static("none", id="sb_subagent_status")
                yield Label("TOKEN BUDGET", classes="sb_title")
                yield Static("0 / 32,000  (0.0%)", id="sb_tokens")
                yield Label("TOKEN BREAKDOWN", classes="sb_title")
                yield Static("In: 0 | Out: 0", id="sb_context")
                yield Label("SESSION COST", classes="sb_title")
                yield Static("$0.000", id="sb_cost")
                yield Label("PROVIDER / MODEL", classes="sb_title")
                yield Static("disconnected", id="sb_provider")
                yield Static("—", id="sb_model_info")
                yield Label("GIT", classes="sb_title")
                yield Static("○ —", id="sb_git")
                yield Label("WORKING DIR", classes="sb_title")
                yield Static(".", id="sb_workdir")
                yield Label("ACTIVE ROLE", classes="sb_title")
                yield Static("system", id="sb_role")
                yield Label("TOOLS CALLED", classes="sb_title")
                yield Static("0", id="sb_tool_count")
                yield Label("SESSION", classes="sb_title")
                yield Static("Pending: 0 | Queue: 0", id="sb_session")
                yield Static("Status: idle", id="sb_status")
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
            yield Button("", id="subagent_footer_chip", variant="default")

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        from .components import ConsolePanel, FilePickerOverlay

        self._bridge.setup_subscriptions()
        self._bridge.load_history()
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
        try:
            inp = self.query_one("#user_input", ChatTextArea)
            inp._prompt_history = self._bridge.load_prompt_history()
        except Exception:
            pass
        self.post_message(RequestSystemSettings())
        self._bridge.publish_session_request()
        logger.info("TUI mounted and ready")

    def on_unmount(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
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
        self._save_session_snapshot()
        self.exit()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _append_log_line(self, line: str, level: str = "INFO") -> None:
        try:
            from .components import ConsolePanel

            console = self.query_one("#console_panel", ConsolePanel)
            console.write_line(line, level)
        except Exception:
            pass

    # ── EventBus / bus event handlers ─────────────────────────────────────

    @on(RequestSystemSettings)
    def handle_request_system_settings(self, _: RequestSystemSettings) -> None:
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
        except Exception:
            pass

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
            "action_check_providers": lambda: self.notify("Provider check not connected"),
            "action_compact_memory": lambda: self.post_message(SlashCommand("compact")),
        }
        fn = dispatch.get(cmd)
        if fn:
            fn()
        else:
            logger.debug(f"Unhandled palette command: {cmd}")

    def _activate_provider(self, provider_id: str) -> bool:
        try:
            _lm = _load_llm_manager_module()
            get_provider_manager = _lm.get_provider_manager
            canonical_provider = _lm.canonical_provider
            resolve_config_path = _lm.resolve_config_path
            _providers_json_lock = _lm._providers_json_lock

            norm_id = canonical_provider(provider_id)
            pm = get_provider_manager()

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
                logger.warning(f"_activate_provider: no providers.json entry matched '{norm_id}'")

            logger.info(f"_activate_provider: persisted active={norm_id} to providers.json")

            adapter = pm.get_provider(norm_id)
            orch = getattr(self._bridge, "_orchestrator", None)
            if orch is not None:
                orch.adapter = adapter
                logger.info(f"_activate_provider: swapped orchestrator adapter to {norm_id} ({adapter})")
            else:
                logger.warning("_activate_provider: no orchestrator found on bridge")

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
        _COPILOT_IDS = {"github_copilot", "githubcopilot", "github-copilot"}
        if event.provider_id.lower().replace("-", "_") in _COPILOT_IDS:
            from .features.oauth.screen import DeploymentSelectScreen

            self.push_screen(DeploymentSelectScreen())
            return

        try:
            _lm = _load_llm_manager_module()
            get_provider_manager = _lm.get_provider_manager
            canonical_provider = _lm.canonical_provider

            pm = get_provider_manager()
            adapter = pm.get_provider(canonical_provider(event.provider_id))
            if adapter is not None:
                logger.info(f"handle_connect_provider: activating already-configured provider {event.provider_id}")
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
            ProviderConfigScreen(event.provider_id, name, existing_base_url=existing_base_url)
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
            self.notify(f"Credentials for {event.provider_id} saved", severity="information")
        except Exception as exc:
            logger.error(f"Failed to persist credentials: {exc}")
            self.notify(f"Error saving credentials: {exc}", severity="error")
            return

        self._activate_provider(event.provider_id)

    @on(UpdateRoleModel)
    def handle_update_role_model(self, event: UpdateRoleModel) -> None:
        logger.info(f"Role model update: {event.role} → {event.model_id} (provider={event.provider_id})")

        if event.provider_id:
            try:
                _lm = _load_llm_manager_module()
                canonical_provider = _lm.canonical_provider

                norm_req = canonical_provider(event.provider_id)
                orch = getattr(self._bridge, "_orchestrator", None)
                current_adapter = getattr(orch, "_adapter", None) if orch else None

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
                    orch = getattr(self._bridge, "_orchestrator", None)
                    current_adapter = getattr(orch, "_adapter", None) if orch else None
            except Exception as _exc:
                logger.debug(f"handle_update_role_model provider switch: {_exc}")

        try:
            orch = getattr(self._bridge, "_orchestrator", None)
            if orch is not None:
                adapter = getattr(orch, "_adapter", None)
                if adapter is not None:
                    def _valid_str(x: object) -> bool:
                        return (
                            isinstance(x, str)
                            and bool(x.strip())
                            and ("MagicMock" not in x)
                        )

                    model_id = None
                    try:
                        if isinstance(event.model_id, str) and _valid_str(event.model_id):
                            model_id = event.model_id.strip()
                    except Exception:
                        model_id = None

                    if not model_id:
                        logger.debug(f"handle_update_role_model: ignoring invalid model id: {event.model_id}")
                    else:
                        for _attr in ("default_model", "model", "_model"):
                            if hasattr(adapter, _attr):
                                try:
                                    setattr(adapter, _attr, model_id)
                                    logger.info(f"handle_update_role_model: set adapter.{_attr} = {model_id}")
                                except Exception:
                                    logger.debug(f"handle_update_role_model: failed to set adapter.{_attr}")
                                break

                        if hasattr(adapter, "models") and isinstance(adapter.models, list):
                            try:
                                valid_models = [m for m in adapter.models if _valid_str(m)]
                                adapter.models[:] = valid_models
                                if model_id in adapter.models:
                                    adapter.models.remove(model_id)
                                adapter.models.insert(0, model_id)
                            except Exception:
                                try:
                                    if model_id not in adapter.models:
                                        adapter.models.insert(0, model_id)
                                except Exception as _model_ins_err:
                                    logger.debug("model insert best-effort failed: %s", _model_ins_err)

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

    # ── GitHub Copilot OAuth device flow ──────────────────────────────────

    @on(StartGithubDeviceFlow)
    async def handle_start_github_device_flow(self, event: StartGithubDeviceFlow) -> None:
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

        try:
            flow = start_device_flow(enterprise_url=enterprise_url)
        except Exception as exc:
            logger.error(f"start_device_flow failed: {exc}")
            self.notify(f"Failed to start GitHub login: {exc}", severity="error")
            return

        self._device_flow_cancel.clear()

        from .features.oauth.screen import OAuthDeviceFlowScreen

        oauth_screen = OAuthDeviceFlowScreen(
            user_code=flow.user_code,
            verification_uri=flow.verification_uri,
            expires_in=flow.expires_in,
        )
        self._oauth_screen = oauth_screen
        await self.push_screen(oauth_screen)

        cancel_evt = self._device_flow_cancel
        domain = flow.domain

        def _poll_thread() -> None:
            try:
                token = poll_for_token(
                    flow.device_code, flow.interval, domain=domain,
                    timeout=flow.expires_in, cancel_event=cancel_evt,
                )
                save_token(token, enterprise_url=enterprise_url)
                self.call_from_thread(self.post_message, DeviceFlowCompleteEvent(provider_id="github_copilot"))
            except DeviceCodeExpired:
                self.call_from_thread(self.post_message, DeviceFlowErrorEvent(reason="Device code expired — please try again."))
            except AuthCancelled:
                self.call_from_thread(self.post_message, DeviceFlowErrorEvent(reason="Authorization was denied on GitHub."))
            except Exception as exc:
                if cancel_evt.is_set():
                    return
                self.call_from_thread(self.post_message, DeviceFlowErrorEvent(reason=str(exc)))

        threading.Thread(target=_poll_thread, daemon=True, name="copilot-poll").start()

    @on(DeviceFlowCompleteEvent)
    def handle_device_flow_complete(self, event: DeviceFlowCompleteEvent) -> None:
        if self._oauth_screen is not None:
            try:
                screen = self._oauth_screen
                self._oauth_screen = None
                screen.complete()
            except Exception as _oauth_err:
                logger.debug("dismiss OAuth screen failed: %s", _oauth_err)

        try:
            from .features.settings.screen import SettingsScreen

            for screen in list(self.screen_stack):
                if isinstance(screen, SettingsScreen):
                    screen.dismiss()
                    break
        except Exception as _settings_err:
            pass

        provider_id = getattr(event, "provider_id", "github_copilot") or "github_copilot"
        prov = self._settings.get_provider_by_id(provider_id)
        provider_name = prov["name"] if prov else provider_id.replace("_", " ").title()
        try:
            self.post_message(
                ProviderStatusChangeEvent(
                    provider=provider_name, new_status="connected", old_status="disconnected",
                )
            )
        except Exception:
            pass

        self.notify(
            f"{provider_name} connected! Reopen Settings to see updated status.",
            severity="information", timeout=6,
        )
        logger.info("GitHub Copilot OAuth: token saved successfully")

    @on(DeviceFlowErrorEvent)
    def handle_device_flow_error(self, event: DeviceFlowErrorEvent) -> None:
        self._device_flow_cancel.set()
        if self._oauth_screen is not None:
            try:
                screen = self._oauth_screen
                self._oauth_screen = None
                screen.fail(event.reason)
            except Exception:
                pass
        if event.reason and event.reason != "cancelled":
            self.notify(f"GitHub Copilot login failed: {event.reason}", severity="error", timeout=8)

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
        from .features.palette.screen import CommandPalette

        self.push_screen(CommandPalette(self._settings, initial_action="menu_switch_model"))

    def action_open_settings(self) -> None:
        try:
            from .features.settings.screen import SettingsScreen

            self.push_screen(SettingsScreen(self._settings))
        except Exception as exc:
            logger.exception(f"action_open_settings failed: {exc}")
            self.notify(f"Could not open settings: {exc}", severity="error")

    def action_open_sessions(self) -> None:
        from .screens.session_screen import SessionScreen

        self.push_screen(SessionScreen())

    def _clear_session(self) -> None:
        self._clear_chat_panel()
        self._reset_sidebar()
        self.notify("Session cleared")
