"""
AgentApp — TUI System Specification v2.0 compliant Textual application.
Wires exclusively through AgentBridge → EventBus; never imports src.core directly.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .components import FilePickerOverlay
    from .components import SubagentProgress
    from .features.oauth.screen import OAuthDeviceFlowScreen

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
from .palette_provider import CodingAgentCommands
from .settings import SettingsStore

logger = get_logger("app")

ROLE_LABELS = _SB_ROLE_LABELS
ROLE_COLORS = _SB_ROLE_COLORS
_SIDEBAR_MIN_COLS = 110


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
    ENABLE_COMMAND_PALETTE = True
    COMMANDS = App.COMMANDS | {CodingAgentCommands}
    BINDINGS = [
        ("shift+tab", "toggle_plan_mode", "plan"),
        ("ctrl+b", "toggle_sidebar", "sidebar"),
        ("ctrl+l", "toggle_console", "console"),
        ("ctrl+o", "show_commands", "commands"),
        ("ctrl+s", "open_settings", "settings"),
        ("ctrl+m", "open_model_picker", "model"),
        ("ctrl+q", "quit_app", "quit"),
        ("ctrl+r", "open_sessions", "sessions"),
    ]
    CSS_PATH = "styles/app.tcss"

    active_role = reactive("lead_architect")
    total_tokens = reactive(0)
    context_window = reactive(32000)
    pending_tasks = reactive(0)
    queue_size = reactive(0)
    is_streaming = reactive(False)
    agent_running = reactive(False)

    def __init__(
        self,
        working_dir: Optional[Path] = None,
        trusted: Optional[bool] = None,
        plugin_dir: Optional[str] = None,
    ) -> None:
        super().__init__()
        import uuid as _uuid

        from .core_bridge import AgentBridge

        if working_dir is not None:
            self._initial_working_dir = working_dir
        if trusted is not None:
            self._initial_trusted = trusted
        if plugin_dir is not None:
            self._initial_plugin_dir = plugin_dir

        _init_wd: Optional[Path] = getattr(self, "_initial_working_dir", None)
        self._bridge = AgentBridge(self, working_dir=_init_wd)
        self._session_id: str = str(_uuid.uuid4())
        self._session_custom_title: Optional[str] = None
        self._current_stream = None  # type: ignore[assignment]
        self._active_stream_view = None  # type: ignore[assignment]
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
        self._oauth_screen: Optional[OAuthDeviceFlowScreen] = None
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
                yield Button("↓ Jump to latest", id="jump_to_latest_btn", variant="primary")
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
                init_limit = getattr(self, "context_window", 32000) or 32000
                yield Static(f"0 / {init_limit:,}  (0.0%)", id="sb_tokens")
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
                "shift+tab [b]plan[/b]  ctrl+b [b]sidebar[/b]  "
                "ctrl+p [b]palette[/b]  ctrl+s [b]settings[/b]  "
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

        # TASK-37: Do not block startup with a modal unless user explicitly opts in.
        try:
            if self._settings.get("show_sessions_on_startup", False):
                _sessions_dir = self._get_sessions_dir()
                if _sessions_dir and any(_sessions_dir.iterdir()):
                    from .screens.session_screen import SessionScreen
                    self.call_later(self.push_screen, SessionScreen())
        except Exception:
            pass

        # TASK-38: Responsive initial sidebar check for narrow terminals (< 110 cols)
        try:
            self._apply_responsive_sidebar(self.size.width)
        except Exception:
            pass

        logger.info("TUI mounted and ready")

    def _apply_responsive_sidebar(self, width: int) -> None:
        if getattr(self, "_sidebar_user_override", None) is not None:
            return
        try:
            sidebar = self.query_one("#right_sidebar", VerticalScroll)
            sidebar.set_class(width < _SIDEBAR_MIN_COLS, "hidden")
        except Exception:
            pass

    def on_resize(self, event) -> None:
        try:
            self._apply_responsive_sidebar(int(event.size.width))
        except Exception:
            pass

    @on(Button.Pressed, "#jump_to_latest_btn")
    def _on_jump_to_latest(self) -> None:
        try:
            self.query_one("#chat_log", VerticalScroll).scroll_end(animate=False)
            self._set_jump_banner_visible(False)
        except Exception:
            pass

    def on_unmount(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        # TASK-37: Discard pending queued prompts on quit instead of dispatching LLM runs during teardown.
        if getattr(self, "_queued_messages", None):
            dropped = len(self._queued_messages)
            self._queued_messages.clear()
            logger.info("Discarded %d queued message(s) on shutdown", dropped)
        self._save_session_snapshot()
        try:
            if hasattr(self, "_bridge") and self._bridge is not None and hasattr(self._bridge, "cleanup"):
                self._bridge.cleanup()
        except Exception:
            pass
        if hasattr(self, "_worktree_mgr") and self._worktree_mgr is not None:
            try:
                import subprocess
                for wt_path in list(getattr(self._worktree_mgr, "active", {}).values()):
                    try:
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", str(wt_path)],
                            capture_output=True,
                            timeout=10,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        self._bridge.interrupt()
        self._bridge.save_history()
        self._bridge.cleanup()
        logger.info("TUI unmounted — bridge cleaned up")

    def action_quit_app(self) -> None:
        self._save_session_snapshot()
        try:
            if hasattr(self, "_bridge") and self._bridge is not None and hasattr(self._bridge, "cleanup"):
                self._bridge.cleanup()
        except Exception:
            pass
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
        saved_theme = self._settings.get("theme") or event.settings.get("theme", "textual-dark")
        if not getattr(self, "_theme_user_selected", False):
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

        if cmd.startswith("slash:"):
            slash_cmd = cmd[len("slash:") :]
            parts = slash_cmd.split(maxsplit=1)
            command = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            self.post_message(SlashCommand(command, args))
            return

        dispatch = {
            "action_toggle_console": self.action_toggle_console,
            "action_open_settings": self.action_open_settings,
            "action_toggle_sidebar": self.action_toggle_sidebar,
            "action_quit": self.action_quit_app,
            "action_new_session": lambda: self.post_message(SlashCommand("new")),
            "action_check_providers": lambda: self.post_message(SlashCommand("doctor")),
            "action_compact_memory": lambda: self.post_message(SlashCommand("compact")),
            "action_open_sessions": self.action_open_sessions,
            "action_open_editor": self.action_open_editor,
        }
        fn = dispatch.get(cmd)
        if fn:
            fn()
        elif hasattr(self, cmd) and callable(getattr(self, cmd)):
            getattr(self, cmd)()
        else:
            logger.debug(f"Unhandled palette command: {cmd}")

    def _activate_provider(self, provider_id: str) -> bool:
        return bool(self._bridge.activate_provider(provider_id))

    @on(ConnectProvider)
    def handle_connect_provider(self, event: ConnectProvider) -> None:
        _COPILOT_IDS = {"github_copilot", "githubcopilot", "github-copilot"}
        if event.provider_id.lower().replace("-", "_") in _COPILOT_IDS:
            from .features.oauth.screen import DeploymentSelectScreen

            self.push_screen(DeploymentSelectScreen())
            return

        if self._bridge.is_provider_configured(event.provider_id):
            self._activate_provider(event.provider_id)
            self.notify(
                f"Switched to {event.provider_id.replace('_', ' ').title()}",
                severity="information",
            )
            return

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
        self._bridge.update_role_model(event.model_id, provider_id=event.provider_id)

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
            _auth = self._bridge.load_copilot_auth_module()
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

        import asyncio
        try:
            flow = await asyncio.to_thread(start_device_flow, enterprise_url=enterprise_url)
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

            for _scr in list(self.screen_stack):
                if isinstance(_scr, SettingsScreen):
                    _scr.dismiss()
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
        if hasattr(self, "_bridge") and self._bridge is not None:
            try:
                self._bridge.set_active_role(new_role)
            except Exception:
                pass
        self.notify(f"Switched to {ROLE_LABELS.get(new_role, new_role)}")

    def watch_active_role(self, old_role: str, new_role: str) -> None:
        if hasattr(self, "_bridge") and self._bridge is not None:
            try:
                self._bridge.set_active_role(new_role)
            except Exception:
                pass
        if hasattr(self, "_role_cycle") and new_role in self._role_cycle:
            self._role_idx = self._role_cycle.index(new_role)

    def watch_context_window(self, new_limit: int) -> None:
        if new_limit > 0:
            used_pct = (self.total_tokens / new_limit * 100) if new_limit else 0.0
            from ._app_status_handlers_mixin import _budget_color

            color = _budget_color(used_pct)
            try:
                self.query_one("#sb_tokens", Static).update(
                    f"[bold {color}]{self.total_tokens:,} / {new_limit:,}  ({used_pct:.1f}%)[/]"
                )
            except Exception:
                pass
            if hasattr(self, "_update_status_bar"):
                self._update_status_bar()

    def watch_total_tokens(self, new_tokens: int) -> None:
        limit = self.context_window or 32000
        used_pct = (new_tokens / limit * 100) if limit else 0.0
        from ._app_status_handlers_mixin import _budget_color

        color = _budget_color(used_pct)
        try:
            self.query_one("#sb_tokens", Static).update(
                f"[bold {color}]{new_tokens:,} / {limit:,}  ({used_pct:.1f}%)[/]"
            )
        except Exception:
            pass
        if hasattr(self, "_update_status_bar"):
            self._update_status_bar()

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

    def action_toggle_plan_mode(self) -> None:
        """Toggle planning vs execution mode via Shift+Tab (TASK-38 — frees Tab for completion/focus)."""
        cur = getattr(self, "_plan_mode_active", False)
        self._plan_mode_active = not cur
        mode_name = "planning" if self._plan_mode_active else "execution"
        self.active_role = "strategic" if self._plan_mode_active else "operational"
        self._update_role_display(self.active_role)
        self._update_status_bar()
        self.notify(f"Mode: {mode_name}")

    def action_toggle_sidebar(self) -> None:
        try:
            sidebar = self.query_one("#right_sidebar", VerticalScroll)
            now_hidden = "hidden" not in sidebar.classes
            sidebar.set_class(now_hidden, "hidden")
            self._sidebar_user_override = not now_hidden
        except Exception as e:
            logger.error(f"Error toggling sidebar: {e}")

    def action_show_commands(self) -> None:
        self.action_command_palette()

    action_open_palette = action_show_commands

    def action_open_model_picker(self) -> None:
        self.post_message(SlashCommand("model", ""))

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

    def action_open_editor(self) -> None:
        import os

        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            self.notify("No $EDITOR or $VISUAL configured in environment", severity="warning")
            return
        self.notify(f"External editor configured: {editor}", severity="information")

    def _clear_session(self) -> None:
        self._clear_chat_panel()
        self._reset_sidebar()
        self.notify("Session cleared")

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _register_mixin_decorated_handlers(cls)


def _register_mixin_decorated_handlers(app_cls: type) -> None:
    """Populate ``app_cls._decorated_handlers`` with ``@on(...)`` methods from plain mixins.

    Textual's ``_MessagePumpMeta.__new__`` scans only ``class_dict`` of the
    MessagePump class itself, so ``@on(...)`` decorators on mixins inheriting
    from ``AgentAppProtocol`` (plain ``type``) are otherwise ignored at runtime.
    """
    handlers = app_cls.__dict__.get("_decorated_handlers")
    if handlers is None:
        handlers = {}
        app_cls._decorated_handlers = handlers  # type: ignore[attr-defined]

    seen_names: set[str] = set()
    for base in app_cls.__mro__:
        if base is object:
            continue
        is_message_pump_base = base is not app_cls and "_decorated_handlers" in base.__dict__
        for attr_name, value in base.__dict__.items():
            if attr_name in seen_names:
                continue
            seen_names.add(attr_name)
            if is_message_pump_base:
                continue
            if callable(value) and hasattr(value, "_textual_on"):
                for message_type, selectors in getattr(value, "_textual_on"):
                    bucket = handlers.setdefault(message_type, [])
                    if not any(m is value for m, _ in bucket):
                        bucket.append((value, selectors))


_register_mixin_decorated_handlers(AgentApp)
