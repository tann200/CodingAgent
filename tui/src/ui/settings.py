import json
from pathlib import Path
from typing import Any, Dict, List, Optional


CONFIG_DIR = Path.home() / ".agent_tui"
CONFIG_FILE = CONFIG_DIR / "settings.json"

AGENTS = [
    {"id": "lead_architect", "label": "Lead Architect"},
    {"id": "full_stack_engineer", "label": "Full Stack Engineer"},
    {"id": "qa_lead", "label": "QA Lead"},
]

TEXTUAL_THEMES = [
    "textual-dark", "textual-light", "nord", "gruvbox", "catppuccin-mocha",
    "dracula", "tokyo-night", "monokai", "flexoki", "solarized-dark",
    "solarized-light", "rose-pine", "rose-pine-moon", "atom-one-dark",
    "atom-one-light", "catppuccin-latte", "catppuccin-frappe",
    "catppuccin-macchiato", "rose-pine-dawn", "textual-ansi",
]

DEFAULTS: Dict[str, Any] = {
    "theme": "textual-dark",
    "default_provider": "none",
    "default_model": "none",
    "lead_architect_provider": "none",
    "lead_architect_model": "none",
    "full_stack_engineer_provider": "none",
    "full_stack_engineer_model": "none",
    "qa_lead_provider": "none",
    "qa_lead_model": "none",
    "console_visible": False,
    "sidebar_visible": True,
    "context_window": 32000,
    "active_mode": "lead_architect",
}


class SettingsStore:
    def __init__(self, path: Optional[Path] = None):
        self._path = path or CONFIG_FILE
        self._data: Dict[str, Any] = {}
        self._available_providers: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        self._data = dict(DEFAULTS)
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._data.update(saved)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def apply_system_settings(self, settings_dict: Dict[str, Any], providers: List[Dict[str, Any]]) -> None:
        for k, v in settings_dict.items():
            if k not in self._data or self._data[k] == DEFAULTS.get(k):
                self._data[k] = v
        self._available_providers = providers

    @property
    def available_providers(self) -> List[Dict[str, Any]]:
        return self._available_providers

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, updates: Dict[str, Any]) -> None:
        self._data.update(updates)

    def get_agent_provider(self, agent_id: str) -> str:
        return self._data.get(f"{agent_id}_provider", self._data.get("default_provider", "none"))

    def get_agent_model(self, agent_id: str) -> str:
        return self._data.get(f"{agent_id}_model", self._data.get("default_model", ""))

    def get_provider_by_id(self, provider_id: str) -> Optional[Dict[str, Any]]:
        for p in self._available_providers:
            pid = p["name"].lower().replace(" ", "_")
            if pid == provider_id or p["name"].lower() == provider_id:
                return p
        return None

    def get_models_for_provider(self, provider_name: str) -> List[str]:
        for p in self._available_providers:
            if p["name"].lower() == provider_name.lower() or p["name"].lower().replace(" ", "_") == provider_name:
                return p.get("models", [])
        return []

    def get_api_key(self, provider_id: str) -> str:
        """Return stored API key for provider_id from providers.json, or empty string."""
        try:
            from src.ui.config_writer import load_provider_credentials
            return load_provider_credentials(provider_id).get("api_key", "")
        except Exception:
            return self._data.get(f"{provider_id}_api_key", "")

    def get_all_models_flat(self) -> List[Dict[str, str]]:
        results = []
        for prov in self._available_providers:
            prov_name = prov["name"]
            for model in prov.get("models", []):
                results.append({"provider_name": prov_name, "model": model})
        return results

    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"No setting: {name}")
