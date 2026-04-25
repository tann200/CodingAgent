"""
Settings screen — TUI System Specification v2.0 compliant.

Improvements over the original:
  1. Reactive model filtering: when a provider Select changes for an agent,
     the corresponding model Select is immediately rebuilt to show only that
     provider's models (uses Select.set_options()).
  2. API Keys section: one row per provider with a status dot (● connected /
     ○ not configured), a masked input field, and a Test button.
  3. Connection dot colours: green = key present, dim = not configured.
   4. GitHub Copilot: shows "Login with GitHub Copilot" OAuth button instead
      of a generic API key input.  Opens DeploymentSelectScreen (deployment
      type + optional GHE URL), which then fires StartGithubDeviceFlow.
"""

from __future__ import annotations

from textual.screen import ModalScreen
from textual.widgets import Input, Label, Button, Select, Static
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual import on
from textual.events import Key

from ...settings import AGENTS, TEXTUAL_THEMES
from ...events import (
    UpdateSettings,
    SaveProviderCredentials,
    UpdateRoleModel,
)
from ...logging import get_logger

logger = get_logger("settings")

_NO_PROVIDER = "none"
_GITHUB_COPILOT_PID = "github_copilot"

# Provider types that run locally and do not require an API key.
# For these we show a base URL field instead of an API key input.
_LOCAL_PROVIDER_TYPES = {"lm_studio", "ollama", "openai_compat", "local"}


def _pid(name: str) -> str:
    return name.lower().replace(" ", "_")


def _is_local_provider(p: dict) -> bool:
    """Return True if *p* is a local/self-hosted provider that needs no API key."""
    ptype = (p.get("type") or "").lower().replace("-", "_")
    return ptype in _LOCAL_PROVIDER_TYPES


def _dot(has_key: bool) -> str:
    return "[bold #22c55e]●[/]" if has_key else "[dim]○[/]"


class SettingsScreen(ModalScreen):
    BINDINGS = [("escape", "cancel", "Close")]

    def __init__(self, settings_store) -> None:
        super().__init__()
        self.settings = settings_store
        # Build providers lookup: {provider_id: {name, models, ...}}
        # Filter out any entries with empty/missing names to avoid silent key errors.
        self._providers: list[dict] = [
            p for p in (settings_store.available_providers or []) if p.get("name")
        ]
        self._prov_models: dict[str, list[str]] = {
            _pid(p["name"]): list(p.get("models", [])) for p in self._providers
        }
        # Check Copilot auth state once at construction time (cheap — no network call)
        self._copilot_authenticated: bool = self._check_copilot_auth()

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self):
        logger.debug(
            f"SettingsScreen.compose() start  providers={[p['name'] for p in self._providers]}"
            f"  copilot_auth={self._copilot_authenticated}"
        )
        try:
            yield from self._compose_inner()
        except Exception as exc:
            # Preserve ERROR-level stack trace information using logger.exception
            logger.exception("SettingsScreen.compose() raised: %s", exc)
            from textual.widgets import Label as _Label

            yield _Label(f"[bold red]Settings error:[/] {exc}", markup=True)

    def _compose_inner(self):
        prov_options = [(_NO_PROVIDER, _NO_PROVIDER)] + [
            (p["name"], _pid(p["name"])) for p in self._providers
        ]

        with Vertical(id="settings_box"):
            yield Label("Settings", classes="settings_title")
            with VerticalScroll(id="settings_scroll"):
                # ── General ───────────────────────────────────────────────
                yield Label("General", classes="section_title")
                yield Label("Theme", classes="field_label")
                current_theme = self.settings.get("theme", "textual-dark")
                theme_options = [(t, t) for t in TEXTUAL_THEMES]
                yield Select(
                    theme_options,
                    value=current_theme
                    if current_theme in TEXTUAL_THEMES
                    else "textual-dark",
                    id="field_theme",
                    allow_blank=False,
                )

                # ── Display — GAP-CONFIG-1/2/3 ────────────────────────────
                yield Label("Display", classes="section_title")

                # GAP-CONFIG-1: diff style
                yield Label("Diff style", classes="field_label")
                current_diff_style = self.settings.get("diff_style", "side-by-side")
                yield Select(
                    [("Side by side", "side-by-side"), ("Inline", "inline")],
                    value=current_diff_style
                    if current_diff_style in ("side-by-side", "inline")
                    else "side-by-side",
                    id="field_diff_style",
                    allow_blank=False,
                )

                # GAP-CONFIG-2: scroll speed
                yield Label("Scroll speed (1–10)", classes="field_label")
                yield Input(
                    value=str(self.settings.get("scroll_speed", 3)),
                    id="field_scroll_speed",
                    placeholder="3",
                )

                # GAP-CONFIG-3: conceal sensitive values
                yield Label("Conceal sensitive values", classes="field_label")
                conceal = self.settings.get("conceal_sensitive", False)
                yield Select(
                    [("Off", "false"), ("On", "true")],
                    value="true" if conceal else "false",
                    id="field_conceal_sensitive",
                    allow_blank=False,
                )

                # ── Agent role configuration ───────────────────────────────
                for agent in AGENTS:
                    aid = agent["id"]
                    yield Label(agent["label"], classes="section_title")

                    current_prov = self.settings.get_agent_provider(aid) or _NO_PROVIDER
                    if current_prov not in [v for _, v in prov_options]:
                        current_prov = _NO_PROVIDER

                    yield Label("Provider", classes="field_label")
                    yield Select(
                        list(prov_options),
                        value=current_prov,
                        id=f"field_{aid}_provider",
                        allow_blank=False,
                    )

                    yield Label("Model", classes="field_label")
                    model_options = self._build_model_options(current_prov)
                    current_model = self.settings.get_agent_model(aid) or _NO_PROVIDER
                    if current_model not in [v for _, v in model_options]:
                        current_model = _NO_PROVIDER
                    yield Select(
                        model_options,
                        value=current_model,
                        id=f"field_{aid}_model",
                        allow_blank=False,
                    )

                # ── Context window ─────────────────────────────────────────
                yield Label("Context", classes="section_title")
                yield Label("Context Window Size", classes="field_label")
                ctx = str(self.settings.get("context_window", 32000))
                ctx_options = [
                    ("8,000", "8000"),
                    ("16,000", "16000"),
                    ("32,000", "32000"),
                    ("64,000", "64000"),
                    ("128,000", "128000"),
                    ("200,000", "200000"),
                ]
                ctx_val = ctx if ctx in [v for _, v in ctx_options] else "32000"
                yield Select(
                    ctx_options,
                    value=ctx_val,
                    id="field_context_window",
                    allow_blank=False,
                )

                # ── API Keys / Provider Auth ───────────────────────────────
                if self._providers:
                    yield Label("API Keys / Provider Auth", classes="section_title")
                    for p in self._providers:
                        pid = _pid(p["name"])
                        if pid == _GITHUB_COPILOT_PID:
                            # GitHub Copilot uses OAuth device flow, not an API key
                            authenticated = self._copilot_authenticated
                            with Horizontal(classes="apikey_row"):
                                yield Static(
                                    _dot(authenticated),
                                    id=f"dot_{pid}",
                                    classes="apikey_dot",
                                    markup=True,
                                )
                                yield Label(p["name"], classes="apikey_name")
                                if authenticated:
                                    yield Static(
                                        "[bold #22c55e]Connected[/]",
                                        id="copilot_status",
                                        classes="apikey_status",
                                        markup=True,
                                    )
                                    yield Button(
                                        "Disconnect",
                                        id="copilot_logout_btn",
                                        classes="apikey_logout_btn",
                                        variant="warning",
                                    )
                                else:
                                    yield Button(
                                        "Login with GitHub Copilot",
                                        id="copilot_login_btn",
                                        classes="apikey_login_btn",
                                        variant="primary",
                                    )
                        elif _is_local_provider(p):
                            # Local providers (LM Studio, Ollama, …) need no API key —
                            # just show the base URL so the user can change the endpoint.
                            current_url = p.get("base_url") or ""
                            with Horizontal(classes="apikey_row"):
                                yield Static(
                                    "[bold #22c55e]●[/]",
                                    id=f"dot_{pid}",
                                    classes="apikey_dot",
                                    markup=True,
                                )
                                yield Label(p["name"], classes="apikey_name")
                                yield Input(
                                    value=current_url,
                                    placeholder="http://localhost:1234/v1",
                                    id=f"baseurl_{pid}",
                                    classes="apikey_input",
                                )
                        else:
                            has_key = bool(self.settings.get_api_key(pid))
                            with Horizontal(classes="apikey_row"):
                                yield Static(
                                    _dot(has_key),
                                    id=f"dot_{pid}",
                                    classes="apikey_dot",
                                    markup=True,
                                )
                                yield Label(p["name"], classes="apikey_name")
                                yield Input(
                                    password=True,
                                    placeholder="sk-… (leave blank to keep current)",
                                    id=f"apikey_{pid}",
                                    classes="apikey_input",
                                )
                                yield Button(
                                    "Test",
                                    id=f"test_{pid}",
                                    classes="apikey_test_btn",
                                )

            with Horizontal(id="settings_actions"):
                yield Button("Save", variant="primary", id="save_btn")
                yield Button("Cancel", id="cancel_btn")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _load_copilot_auth_module():
        """Load github_copilot_auth by absolute file path to avoid the TUI
        sys.modules shadow where ``src`` → ``tui/src`` (not the project root src).

        The module is registered in sys.modules under its fake name so that
        Python's @dataclass decorator works correctly.  Subsequent calls return
        the cached module.
        """
        import importlib.util
        import sys
        from pathlib import Path

        _MOD_NAME = "_copilot_auth_real"
        if _MOD_NAME in sys.modules:
            return sys.modules[_MOD_NAME]

        # screen.py is at tui/src/ui/features/settings/screen.py
        # parents[5] = project root
        auth_path = (
            Path(__file__).parents[5]
            / "src"
            / "core"
            / "inference"
            / "adapters"
            / "github_copilot_auth.py"
        )
        spec = importlib.util.spec_from_file_location(_MOD_NAME, str(auth_path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load spec from {auth_path}")
        mod = importlib.util.module_from_spec(spec)
        # Register BEFORE exec_module so @dataclass can resolve cls.__module__
        sys.modules[_MOD_NAME] = mod
        try:
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        except Exception:
            sys.modules.pop(_MOD_NAME, None)
            raise
        return mod

    @staticmethod
    def _check_copilot_auth() -> bool:
        """Return True if a GitHub Copilot token is already stored. No network call."""
        try:
            mod = SettingsScreen._load_copilot_auth_module()
            return mod.is_authenticated()
        except Exception as exc:
            logger.warning(f"_check_copilot_auth failed: {exc}")
            return False

    def _build_model_options(self, provider_id: str) -> list[tuple[str, str]]:
        """Return model options for the given provider_id (or all if 'none')."""
        opts = [(_NO_PROVIDER, _NO_PROVIDER)]
        if provider_id and provider_id != _NO_PROVIDER:
            for m in self._prov_models.get(provider_id, []):
                opts.append((m, m))
        else:
            # No provider selected → show all models across all providers
            for p in self._providers:
                for m in p.get("models", []):
                    opts.append((f"{p['name']}: {m}", m))
        return opts

    def _update_model_select(self, aid: str, provider_id: str) -> None:
        """Rebuild the model Select for the given agent to match the chosen provider."""
        try:
            sel = self.query_one(f"#field_{aid}_model", Select)
        except Exception:
            return
        opts = self._build_model_options(provider_id)
        sel.set_options(opts)
        sel.value = _NO_PROVIDER

    # ── Reactive model filtering ──────────────────────────────────────────────

    @on(Select.Changed)
    def on_any_select_changed(self, event: Select.Changed) -> None:
        # Theme preview
        if event.select.id == "field_theme":
            if event.value and event.value != Select.BLANK:
                try:
                    self.app.theme = str(event.value)
                except Exception as exc:
                    logger.error(f"Theme preview error: {exc}")
            return

        # Provider changed for an agent → rebuild model options
        for agent in AGENTS:
            aid = agent["id"]
            if event.select.id == f"field_{aid}_provider":
                prov_val = (
                    str(event.value) if event.value != Select.BLANK else _NO_PROVIDER
                )
                self._update_model_select(aid, prov_val)
                return

    # ── API Key Test button ───────────────────────────────────────────────────

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""

        if btn_id == "save_btn":
            self._do_save()
            return

        if btn_id == "cancel_btn":
            self.action_cancel()
            return

        if btn_id == "copilot_login_btn":
            # Push DeploymentSelectScreen on top of settings — matching OpenCode's flow
            # where the connect dialog is shown without dismissing the parent first.
            from ..oauth.screen import DeploymentSelectScreen

            self.app.push_screen(DeploymentSelectScreen())
            return

        if btn_id == "copilot_logout_btn":
            self._do_copilot_logout()
            return

        if btn_id.startswith("test_"):
            pid = btn_id[len("test_") :]
            self._do_test(pid)
            return

    def _do_test(self, provider_id: str) -> None:
        """Quick connectivity test — saves the key first, then pings."""
        try:
            inp = self.query_one(f"#apikey_{provider_id}", Input)
            key = inp.value.strip()
        except Exception:
            key = ""

        if key:
            self.post_message(
                SaveProviderCredentials(
                    provider_id=provider_id,
                    api_key=key,
                )
            )
            # Update the dot to connected
            try:
                dot = self.query_one(f"#dot_{provider_id}", Static)
                dot.update(_dot(True))
            except Exception:
                pass
            self.app.notify(
                f"{provider_id}: key saved — connection test queued",
                severity="information",
                timeout=4,
            )
        else:
            self.app.notify(
                f"{provider_id}: enter a key first",
                severity="warning",
                timeout=3,
            )

    def _do_copilot_logout(self) -> None:
        """Clear the stored GitHub Copilot OAuth token."""
        try:
            mod = self._load_copilot_auth_module()
            mod.clear_token()
            # Update dot + swap button text
            try:
                dot = self.query_one(f"#dot_{_GITHUB_COPILOT_PID}", Static)
                dot.update(_dot(False))
            except Exception:
                pass
            self.app.notify(
                "GitHub Copilot disconnected. Restart to apply.",
                severity="warning",
                timeout=6,
            )
            self.dismiss()
        except Exception as exc:
            logger.error(f"Copilot logout failed: {exc}")
            self.app.notify(f"Logout failed: {exc}", severity="error", timeout=5)

    # ── Save ──────────────────────────────────────────────────────────────────

    def _do_save(self) -> None:
        updates: dict = {}

        # Theme
        try:
            t = self.query_one("#field_theme", Select)
            if t.value and t.value != Select.BLANK:
                updates["theme"] = str(t.value)
        except Exception:
            pass

        # Agent providers / models
        for agent in AGENTS:
            aid = agent["id"]
            try:
                ps = self.query_one(f"#field_{aid}_provider", Select)
                if ps.value and ps.value != Select.BLANK:
                    updates[f"{aid}_provider"] = str(ps.value)
            except Exception:
                pass
            try:
                ms = self.query_one(f"#field_{aid}_model", Select)
                if ms.value and ms.value != Select.BLANK:
                    mv = str(ms.value)
                    updates[f"{aid}_model"] = mv
                    self.post_message(UpdateRoleModel(role=aid, model_id=mv))
            except Exception:
                pass

        # Context window
        try:
            cs = self.query_one("#field_context_window", Select)
            val = cs.value
            if val and val != Select.BLANK and not isinstance(val, type(Select.BLANK)):
                updates["context_window"] = int(str(val))
        except Exception:
            pass

        # GAP-CONFIG-1: diff style
        try:
            ds = self.query_one("#field_diff_style", Select)
            if ds.value and ds.value != Select.BLANK:
                updates["diff_style"] = str(ds.value)
        except Exception:
            pass

        # GAP-CONFIG-2: scroll speed
        try:
            ss_inp = self.query_one("#field_scroll_speed", Input)
            ss_val = int(ss_inp.value.strip() or "3")
            updates["scroll_speed"] = max(1, min(10, ss_val))
        except Exception:
            pass

        # GAP-CONFIG-3: conceal sensitive
        try:
            conc = self.query_one("#field_conceal_sensitive", Select)
            if conc.value and conc.value != Select.BLANK:
                updates["conceal_sensitive"] = str(conc.value) == "true"
        except Exception:
            pass

        # API keys for cloud providers + base URLs for local providers
        for p in self._providers:
            pid = _pid(p["name"])
            if _is_local_provider(p):
                # Local providers: save an updated base URL if the user changed it
                try:
                    inp = self.query_one(f"#baseurl_{pid}", Input)
                    url = inp.value.strip()
                    if url and url != p.get("base_url", ""):
                        self.post_message(
                            SaveProviderCredentials(
                                provider_id=pid,
                                api_key="",
                                base_url=url,
                            )
                        )
                except Exception:
                    pass
            else:
                # Cloud providers that use OAuth (e.g. GitHub Copilot) have no
                # API key input — their auth is handled via the login button.
                if pid == _GITHUB_COPILOT_PID:
                    continue
                # Cloud providers: save API key if the user typed one
                try:
                    inp = self.query_one(f"#apikey_{pid}", Input)
                    key = inp.value.strip()
                    if key:
                        self.post_message(
                            SaveProviderCredentials(
                                provider_id=pid,
                                api_key=key,
                            )
                        )
                        try:
                            self.query_one(f"#dot_{pid}", Static).update(_dot(True))
                        except Exception:
                            pass
                except Exception:
                    pass

        try:
            self.settings.update(updates)
            self.settings.save()
            logger.info(f"Settings saved: {len(updates)} fields")
            self.post_message(UpdateSettings(updates=updates))
            self.dismiss()
        except Exception as exc:
            logger.error(f"Error saving settings: {exc}")
            self.app.notify(f"Error saving settings: {exc}", severity="error")

    # ── Key / cancel ──────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        self.dismiss()

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.action_cancel()


class ProviderConfigScreen(ModalScreen):
    """Generic provider config dialog.

    For local providers (LM Studio, Ollama, …) shows a base-URL field
    pre-filled from the existing providers.json value; no API key is required.
    For cloud providers (OpenAI, Anthropic, …) shows the standard API key input.
    """

    BINDINGS = [("escape", "cancel", "Close")]

    # Provider id strings that are local / self-hosted.
    _LOCAL_IDS = {"lm_studio", "lmstudio", "ollama", "openai_compat", "local"}

    # Default base URLs keyed by canonical provider id.
    _DEFAULT_URLS: dict = {
        "lm_studio": "http://localhost:1234/v1",
        "lmstudio": "http://localhost:1234/v1",
        "ollama": "http://localhost:11434",
    }

    def __init__(
        self,
        provider_id: str,
        provider_name: str,
        existing_base_url: str = "",
    ) -> None:
        super().__init__()
        self.provider_id = provider_id
        self.provider_name = provider_name
        norm = provider_id.lower().replace("-", "_")
        self._is_local = norm in self._LOCAL_IDS
        # Pre-fill with saved value, fallback to known default, then empty.
        self._initial_base_url = existing_base_url or self._DEFAULT_URLS.get(norm, "")

    def compose(self):
        with Vertical(id="config_box"):
            yield Label(f"Connect {self.provider_name}", classes="settings_title")
            if self._is_local:
                yield Label("Base URL", classes="field_label")
                yield Input(
                    value=self._initial_base_url,
                    id="base_url_input",
                    placeholder="http://localhost:1234/v1",
                )
                yield Label(
                    "[dim]No API key required for local providers.[/dim]",
                    id="local_hint",
                )
            else:
                yield Label("Enter API Key", classes="field_label")
                yield Input(password=True, id="api_key_input", placeholder="sk-...")
            yield Label("", id="validation_msg")
            with Horizontal(id="config_actions"):
                yield Button("Save", variant="primary", id="save_btn")
                yield Button("Cancel", id="cancel_btn")

    @on(Button.Pressed, "#save_btn")
    def save_config(self) -> None:
        if self._is_local:
            base_url = self.query_one("#base_url_input", Input).value.strip()
            # Fall back to provider-specific default when left blank.
            if not base_url:
                norm = self.provider_id.lower().replace("-", "_")
                base_url = self._DEFAULT_URLS.get(norm, "http://localhost:1234/v1")
            self.post_message(
                SaveProviderCredentials(
                    provider_id=self.provider_id,
                    api_key="",  # no key needed
                    base_url=base_url,
                )
            )
            logger.info(
                f"Local provider base URL submitted for {self.provider_id}: {base_url}"
            )
            self.app.notify(f"{self.provider_name} connected at {base_url}")
        else:
            key = self.query_one("#api_key_input", Input).value.strip()
            if not key:
                self.query_one("#validation_msg", Label).update(
                    "[red]API key cannot be empty[/red]"
                )
                return
            self.post_message(
                SaveProviderCredentials(
                    provider_id=self.provider_id,
                    api_key=key,
                )
            )
            logger.info(f"Provider credentials submitted for {self.provider_id}")
            self.app.notify(f"{self.provider_name} API key saved")
        self.dismiss()

    @on(Button.Pressed, "#cancel_btn")
    def action_cancel(self) -> None:
        self.dismiss()

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss()
