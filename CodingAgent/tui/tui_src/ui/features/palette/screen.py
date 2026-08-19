from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static, ListView, ListItem
from textual.containers import Vertical, Horizontal
from textual.app import ComposeResult
from textual import on
from rich.text import Text

from ...events import PaletteCommand, ConnectProvider, UpdateRoleModel
from ...logging import get_logger
from .logic import get_all_commands, filter_commands, get_provider_menu, get_model_menu

logger = get_logger("palette")


class CommandItem(ListItem):
    def __init__(self, label: str, hint: str, action: str) -> None:
        super().__init__()
        self.command_label = label
        self.command_hint = hint
        self.command_action = action

    def compose(self) -> ComposeResult:
        t = Text()
        t.append(f"  {self.command_label}", style="bold white")
        if self.command_hint:
            t.append(f"  {self.command_hint}", style="#666666")
        yield Static(t, markup=False)


class CommandPalette(ModalScreen):
    BINDINGS = [("escape", "go_back", "Back/Close")]

    def __init__(self, settings_store, initial_action: str = "") -> None:
        super().__init__()
        self.settings = settings_store
        self._sub_menu: str = ""
        self._sub_title: str = ""
        self._initial_action = initial_action

    def compose(self) -> ComposeResult:
        with Vertical(id="palette_box"):
            with Horizontal(id="palette_header"):
                yield Label(
                    "[bold]Command Palette[/bold]", id="palette_title", markup=True
                )
                yield Label(
                    "[dim]esc to close | type to filter[/dim]",
                    id="palette_close_hint",
                    markup=True,
                )
            yield Input(placeholder="Type a command...", id="palette_input")
            yield ListView(id="palette_list")
            yield Static("", id="palette_footer")

    def on_mount(self) -> None:
        # Jump directly to a sub-menu if an initial_action was requested (TUI-T12).
        if self._initial_action == "menu_switch_model":
            items = get_model_menu(self.settings.available_providers)
            self._sub_menu = "model"
            self._sub_title = "Switch Model"
            self._show_sub_menu(items, "Switch Model")
            inp = self.query_one("#palette_input", Input)
            inp.placeholder = "Filter models…"
        else:
            self._show_root_commands()
        self.query_one("#palette_input", Input).focus()

    def _show_root_commands(self) -> None:
        self._sub_menu = ""
        self._sub_title = ""
        commands = get_all_commands()
        self._render_commands(commands)
        self._update_footer(f"{len(commands)} commands available")
        try:
            self.query_one("#palette_title", Label).update(
                "[bold]Command Palette[/bold]"
            )
        except Exception:
            pass

    def _show_sub_menu(self, items: list[dict], title: str) -> None:
        self._render_commands(items)
        self._update_footer(f"{len(items)} items | esc to go back")
        try:
            self.query_one("#palette_title", Label).update(f"[bold]{title}[/bold]")
        except Exception:
            pass

    def _render_commands(self, commands: list[dict]) -> None:
        lv = self.query_one("#palette_list", ListView)
        lv.clear()
        current_category = ""
        for cmd in commands:
            cat = cmd.get("category", "")
            if cat and cat != current_category and not self._sub_menu:
                current_category = cat
                separator = ListItem(
                    Static(f"[bold #a855f7]{cat}[/bold #a855f7]", markup=True)
                )
                separator.disabled = True
                lv.append(separator)
            item = CommandItem(
                label=cmd["label"],
                hint=cmd.get("hint", ""),
                action=cmd["action"],
            )
            lv.append(item)

    def _update_footer(self, text: str) -> None:
        try:
            self.query_one("#palette_footer", Static).update(f"[dim]{text}[/dim]")
        except Exception:
            pass

    @on(Input.Changed, "#palette_input")
    def filter_options(self, event: Input.Changed) -> None:
        query = event.value.strip()
        providers = self.settings.available_providers
        if self._sub_menu:
            if self._sub_menu == "provider":
                items = get_provider_menu(providers)
            elif self._sub_menu == "model":
                items = get_model_menu(providers)
            else:
                items = []
            if query:
                q = query.lower()
                items = [
                    i
                    for i in items
                    if q in i["label"].lower() or q in i.get("hint", "").lower()
                ]
            self._render_commands(items)
            self._update_footer(f"{len(items)} items | esc to go back")
        else:
            commands = filter_commands(query)
            self._render_commands(commands)
            self._update_footer(
                f"{len(commands)} commands" + (" matching" if query else " available")
            )

    @on(ListView.Selected)
    def handle_selection(self, event: ListView.Selected) -> None:
        item = event.item
        if not isinstance(item, CommandItem):
            return

        action = item.command_action

        if action == "noop":
            return

        if action == "menu_connect_provider":
            self._sub_menu = "provider"
            self._sub_title = "Connect Provider"
            items = get_provider_menu(self.settings.available_providers)
            self._show_sub_menu(items, "Connect Provider")
            inp = self.query_one("#palette_input", Input)
            inp.value = ""
            inp.placeholder = "Select a provider..."
            return

        if action == "menu_switch_model":
            self._sub_menu = "model"
            self._sub_title = "Switch Model"
            items = get_model_menu(self.settings.available_providers)
            self._show_sub_menu(items, "Switch Model")
            inp = self.query_one("#palette_input", Input)
            inp.value = ""
            inp.placeholder = "Select a model..."
            return

        if action.startswith("setup_prov:"):
            parts = action.split(":")
            prov_id = parts[1] if len(parts) > 1 else ""
            prov_name = parts[2] if len(parts) > 2 else prov_id
            logger.info(f"Connect provider: {prov_name} (id={prov_id})")
            self.app.post_message(ConnectProvider(prov_id))
            self.dismiss()
            return

        if action.startswith("select_model:"):
            parts = action.split(":", 2)
            # New format: select_model:<provider_id>:<model_id>
            # Legacy fallback: select_model:<model_id>
            if len(parts) == 3:
                prov_id = parts[1]
                model_id = parts[2]
            else:
                prov_id = ""
                model_id = parts[1]
            active_role = getattr(self.app, "active_role", "lead_architect")
            logger.info(
                f"Model selected: {model_id} (provider={prov_id}) for role {active_role}"
            )
            self.app.post_message(
                UpdateRoleModel(
                    role=active_role, model_id=model_id, provider_id=prov_id
                )
            )
            self.app.notify(f"Model switched to {model_id} for {active_role}")
            self.dismiss()
            return

        logger.info(f"Palette command: {action}")
        self.post_message(PaletteCommand(action))
        self.dismiss()

    def action_go_back(self) -> None:
        if self._sub_menu:
            self._sub_menu = ""
            self._sub_title = ""
            self._show_root_commands()
            inp = self.query_one("#palette_input", Input)
            inp.value = ""
            inp.placeholder = "Type a command..."
        else:
            self.dismiss()
