import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger("settings")


# Cross-platform config directory — prefer core.paths.get_config_dir() when available
def _get_config_dir() -> Path:
    # Use the centralized loader which resolves src/core/paths when available
    from ._core_paths_loader import get_config_dir as _get_config_dir_helper

    return _get_config_dir_helper()


CONFIG_DIR = None  # type: ignore[assignment]
CONFIG_FILE = None  # type: ignore[assignment]

AGENTS = [
    {"id": "lead_architect", "label": "Lead Architect"},
    {"id": "full_stack_engineer", "label": "Full Stack Engineer"},
    {"id": "qa_lead", "label": "QA Lead"},
]

TEXTUAL_THEMES = [
    "textual-dark",
    "textual-light",
    "nord",
    "gruvbox",
    "catppuccin-mocha",
    "dracula",
    "tokyo-night",
    "monokai",
    "flexoki",
    "solarized-dark",
    "solarized-light",
    "rose-pine",
    "rose-pine-moon",
    "atom-one-dark",
    "atom-one-light",
    "catppuccin-latte",
    "catppuccin-frappe",
    "catppuccin-macchiato",
    "rose-pine-dawn",
    "textual-ansi",
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
    # GAP-CONFIG-1: diff render style — "side-by-side" or "inline"
    "diff_style": "side-by-side",
    # GAP-CONFIG-2: mouse/keyboard scroll speed (lines per tick, 1–10)
    "scroll_speed": 3,
    # GAP-CONFIG-3: conceal sensitive values (API keys, tokens) in TUI output
    "conceal_sensitive": False,
}


class SettingsStore:
    def __init__(self, path: Optional[Path] = None):
        # Compute config file lazily via the TUI loader to avoid import-time
        # package-relative imports when conftest aliases tui modules to src.ui.
        if path:
            self._path = path
        else:
            try:
                cfg_dir = _get_config_dir()
            except Exception:
                # Fall back to legacy location
                cfg_dir = Path.home() / ".agent_tui"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            self._path = Path(cfg_dir) / "settings.json"
        self._data: Dict[str, Any] = {}
        self._available_providers: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        self._data = dict(DEFAULTS)
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    self._data.update(saved)
                else:
                    logger.warning(
                        "settings.json: expected dict, got %s — ignoring",
                        type(saved).__name__,
                    )
            except json.JSONDecodeError as e:
                logger.warning("settings.json: invalid JSON — ignoring: %s", e)
            except OSError as e:
                logger.warning("settings.json: read error — ignoring: %s", e)

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except OSError as e:
            logger.error("Failed to save settings to %s: %s", self._path, e)
        except TypeError as e:
            logger.error("Failed to serialize settings (invalid type): %s", e)

    def apply_system_settings(
        self, settings_dict: Dict[str, Any], providers: List[Dict[str, Any]]
    ) -> None:
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
        return self._data.get(
            f"{agent_id}_provider", self._data.get("default_provider", "none")
        )

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
            if (
                p["name"].lower() == provider_name.lower()
                or p["name"].lower().replace(" ", "_") == provider_name
            ):
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
