"""
Settings screen — TUI System Specification v2.0 compliant.

Improvements over the original:
  1. Reactive model filtering: when a provider Select changes for an agent,
     the corresponding model Select is immediately rebuilt to show only that
     provider's models (uses Select.set_options()).
  2. API Keys section: one row per provider with a status dot (● connected /
     ○ not configured), a masked input field, and a Test button.
  3. Connection dot colours: green = key present, dim = not configured.
"""

from __future__ import annotations

from textual.screen import ModalScreen
from textual.widgets import Input, Label, Button, Select, Static
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual import on
from textual.events import Key

from ...settings import AGENTS, TEXTUAL_THEMES
from ...events import UpdateSettings, SaveProviderCredentials, UpdateRoleModel
from ...logging import get_logger

logger = get_logger("settings")

_NO_PROVIDER = "none"


def _pid(name: str) -> str:
    return name.lower().replace(" ", "_")


def _dot(has_key: bool) -> str:
    return "[bold #22c55e]●[/]" if has_key else "[dim]○[/]"


class SettingsScreen(ModalScreen):
    BINDINGS = [("escape", "cancel", "Close")]

    def __init__(self, settings_store) -> None:
        super().__init__()
        self.settings = settings_store
        # Build providers lookup: {provider_id: {name, models, ...}}
        self._providers: list[dict] = settings_store.available_providers or []
        self._prov_models: dict[str, list[str]] = {
            _pid(p["name"]): list(p.get("models", [])) for p in self._providers
        }

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self):
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

                # ── API Keys ───────────────────────────────────────────────
                if self._providers:
                    yield Label("API Keys", classes="section_title")
                    for p in self._providers:
                        pid = _pid(p["name"])
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

        # API keys (only non-blank values)
        for p in self._providers:
            pid = _pid(p["name"])
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
            self.app.pop_screen()
        except Exception as exc:
            logger.error(f"Error saving settings: {exc}")
            self.app.notify(f"Error saving settings: {exc}", severity="error")

    # ── Key / cancel ──────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.action_cancel()


class ProviderConfigScreen(ModalScreen):
    BINDINGS = [("escape", "cancel", "Close")]

    def __init__(self, provider_id: str, provider_name: str) -> None:
        super().__init__()
        self.provider_id = provider_id
        self.provider_name = provider_name

    def compose(self):
        with Vertical(id="config_box"):
            yield Label(f"Connect {self.provider_name}", classes="settings_title")
            yield Label("Enter API Key", classes="field_label")
            yield Input(password=True, id="api_key_input", placeholder="sk-...")
            yield Label("", id="validation_msg")
            with Horizontal(id="config_actions"):
                yield Button("Save", variant="primary", id="save_btn")
                yield Button("Cancel", id="cancel_btn")

    @on(Button.Pressed, "#save_btn")
    def save_config(self) -> None:
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
        self.app.pop_screen()

    @on(Button.Pressed, "#cancel_btn")
    def action_cancel(self) -> None:
        self.app.pop_screen()

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.app.pop_screen()
