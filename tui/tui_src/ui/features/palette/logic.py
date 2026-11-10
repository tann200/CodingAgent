from ...settings import SettingsStore

COMMAND_LIST = [
    # Session category
    {
        "label": "New session",
        "slash": "/new",
        "hint": "Clear chat and reset state",
        "action": "action_new_session",
        "category": "Session",
    },
    {
        "label": "Sessions browser",
        "slash": "/sessions",
        "hint": "Browse and resume saved sessions",
        "action": "action_open_sessions",
        "category": "Session",
    },
    {
        "label": "Fork session",
        "slash": "/fork",
        "hint": "Fork current session to a new independent copy",
        "action": "slash:fork",
        "category": "Session",
    },
    {
        "label": "Clear chat",
        "slash": "/clear",
        "hint": "Clear chat output (keeps history)",
        "action": "slash:clear",
        "category": "Session",
    },
    {
        "label": "Compact session",
        "slash": "/compact",
        "hint": "Reduce memory usage",
        "action": "action_compact_memory",
        "category": "Session",
    },
    {
        "label": "Undo changes",
        "slash": "/undo",
        "hint": "Undo last user message and revert file changes",
        "action": "slash:undo",
        "category": "Session",
    },
    {
        "label": "Continue task",
        "slash": "/continue",
        "hint": "Restore and re-run previous task",
        "action": "slash:continue",
        "category": "Session",
    },
    {
        "label": "Interrupt agent",
        "slash": "/interrupt",
        "hint": "Cancel running agent task",
        "action": "slash:interrupt",
        "category": "Session",
    },
    {
        "label": "Open editor",
        "hint": "Launch external editor",
        "action": "action_open_editor",
        "category": "Session",
    },
    {
        "label": "Export session",
        "slash": "/export",
        "hint": "Export conversation to markdown",
        "action": "slash:export",
        "category": "Session",
    },
    {
        "label": "Session timeline",
        "slash": "/timeline",
        "hint": "View session message timeline",
        "action": "slash:timeline",
        "category": "Session",
    },
    # Git category
    {
        "label": "Review diff",
        "slash": "/diff",
        "hint": "Show working-directory diff since last snapshot",
        "action": "slash:diff",
        "category": "Git",
    },
    {
        "label": "Worktree manager",
        "slash": "/worktree",
        "hint": "Manage git worktree isolation",
        "action": "slash:worktree",
        "category": "Git",
    },
    # System category
    {
        "label": "Status",
        "slash": "/status",
        "hint": "Show agent/provider/model status & runtime policy",
        "action": "slash:status",
        "category": "System",
    },
    {
        "label": "Doctor diagnosis",
        "slash": "/doctor",
        "hint": "Diagnose running local models and tool schemas",
        "action": "slash:doctor",
        "category": "System",
    },
    {
        "label": "Inspect skills",
        "slash": "/skills",
        "hint": "Inspect packaged agent skills",
        "action": "slash:skills",
        "category": "System",
    },
    {
        "label": "MCP servers",
        "slash": "/mcp",
        "hint": "Manage MCP servers",
        "action": "slash:mcp",
        "category": "System",
    },
    {
        "label": "Settings",
        "slash": "/settings",
        "hint": "Open settings (ctrl+s)",
        "action": "action_open_settings",
        "category": "System",
    },
    {
        "label": "Help",
        "slash": "/help",
        "hint": "Show available commands help",
        "action": "slash:help",
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
        "slash": "/quit",
        "hint": "Quit application (ctrl+q)",
        "action": "action_quit",
        "category": "System",
    },
    # Model category
    {
        "label": "Switch model",
        "slash": "/model",
        "hint": "Change active LLM model",
        "action": "menu_switch_model",
        "category": "Model",
    },
    {
        "label": "Toggle fast model",
        "slash": "/fast",
        "hint": "Switch to fastest nano tier model",
        "action": "slash:fast",
        "category": "Model",
    },
    # Provider category
    {
        "label": "Connect provider",
        "slash": "/provider",
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
]


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
        or (cmd.get("slash") and q in cmd["slash"].lower())
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
