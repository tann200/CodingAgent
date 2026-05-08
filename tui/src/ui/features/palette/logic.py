COMMAND_LIST = [
    {
        "label": "New session",
        "hint": "Clear chat and reset state",
        "action": "action_new_session",
        "category": "Session",
    },
    {
        "label": "Open editor",
        "hint": "Launch external editor",
        "action": "action_open_editor",
        "category": "Session",
    },
    {
        "label": "Compact session",
        "hint": "Reduce memory usage",
        "action": "action_compact_memory",
        "category": "Session",
    },
    {
        "label": "Switch model",
        "hint": "Change active LLM model",
        "action": "menu_switch_model",
        "category": "Model",
    },
    {
        "label": "Connect provider",
        "hint": "Add API key for a provider",
        "action": "menu_connect_provider",
        "category": "Provider",
    },
    {
        "label": "Check providers",
        "hint": "Probe provider availability",
        "action": "action_check_providers",
        "category": "Provider",
    },
    {
        "label": "Settings",
        "hint": "Open settings (ctrl+s)",
        "action": "action_open_settings",
        "category": "System",
    },
    {
        "label": "Toggle console",
        "hint": "Show/hide log panel (ctrl+l)",
        "action": "action_toggle_console",
        "category": "System",
    },
    {
        "label": "Toggle sidebar",
        "hint": "Show/hide right sidebar",
        "action": "action_toggle_sidebar",
        "category": "System",
    },
    {
        "label": "Exit",
        "hint": "Quit application (ctrl+q)",
        "action": "action_quit",
        "category": "System",
    },
]
from ...settings import SettingsStore


def get_all_commands() -> list[dict]:
    return list(COMMAND_LIST)


def filter_commands(query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return get_all_commands()
    return [
        cmd
        for cmd in COMMAND_LIST
        if q in cmd["label"].lower()
        or q in cmd["hint"].lower()
        or q in cmd["category"].lower()
    ]





def get_provider_menu(available_providers: list[dict]) -> list[dict]:
    if not available_providers:
        return [
            {
                "label": "No providers available",
                "hint": "System has no providers configured",
                "action": "noop",
            }
        ]
    return [
        {
            "label": p["name"],
            "hint": f"Configure {p['name']} API key",
            "action": f"setup_prov:{SettingsStore._normalize_provider_id(p)}:{p['name']}",
        }
        for p in available_providers
    ]


def get_model_menu(available_providers: list[dict]) -> list[dict]:
    items = []
    for prov in available_providers:
        prov_id = SettingsStore._normalize_provider_id(prov)
        for m in prov.get("models", []):
            items.append(
                {
                    "label": f"{prov['name']}: {m}",
                    "hint": "",
                    # Format: select_model:<provider_id>:<model_id>
                    "action": f"select_model:{prov_id}:{m}",
                }
            )
    if not items:
        return [
            {
                "label": "No models available",
                "hint": "Connect a provider first",
                "action": "menu_connect_provider",
            }
        ]
    return items
