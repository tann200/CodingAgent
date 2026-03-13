import json
from pathlib import Path
from typing import Any, Dict, Optional
import os

DEFAULT_FILENAME = os.getenv('CODINGAGENT_PREFS') or str(Path.home() / '.config' / 'codingagent' / 'prefs.json')


class UserPrefs:
    def __init__(self, data: Optional[Dict[str, Any]] = None, path: Optional[Path] = None):
        self.path = Path(path) if path else Path(DEFAULT_FILENAME)
        self.data = data or {}
        # convenience defaults (do not assume a provider)
        self.selected_model_provider = self.data.get('selected_model_provider')
        self.selected_model_name = self.data.get('selected_model_name')
        self.active_mode = self.data.get('active_mode', 'default')

    @classmethod
    def load(cls, path: Optional[str] = None) -> 'UserPrefs':
        p = Path(path) if path else Path(DEFAULT_FILENAME)
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                d = {}
        else:
            d = {}
        return cls(data=d, path=p)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
        except Exception:
            pass

    def get_provider_setting(self, provider_name: str, key: str) -> Optional[Any]:
        providers = self.data.get('providers', {})
        p = providers.get(provider_name.lower(), {})
        return p.get(key)

    def set_provider_setting(self, provider_name: str, key: str, value: Any) -> None:
        providers = self.data.setdefault('providers', {})
        p = providers.setdefault(provider_name.lower(), {})
        p[key] = value
        # also update attributes
        if key == 'api_key':
            self.save()

    def has_any_api_keys(self) -> bool:
        providers = self.data.get('providers', {})
        for p in providers.values():
            if p.get('api_key'):
                return True
        return False

    def get_provider_key(self, name: str) -> Optional[str]:
        return self.get_provider_setting(name, 'api_key')

    def get_mode_model(self, mode: str) -> Optional[str]:
        modes = self.data.get('modes', {})
        return modes.get(mode)

    def set_mode_model(self, mode: str, model: str) -> None:
        modes = self.data.setdefault('modes', {})
        modes[mode] = model
        self.save()

    def update_provider_config(self, provider_key: str, **kwargs) -> None:
        providers = self.data.setdefault('providers', {})
        p = providers.setdefault(provider_key, {})
        for k, v in kwargs.items():
            p[k] = v
        self.save()

    # legacy compatibility
    @property
    def selected_model_provider(self) -> Optional[str]:
        return self.data.get('selected_model_provider')

    @selected_model_provider.setter
    def selected_model_provider(self, v: str) -> None:
        self.data['selected_model_provider'] = v

    @property
    def selected_model_name(self) -> Optional[str]:
        return self.data.get('selected_model_name')

    @selected_model_name.setter
    def selected_model_name(self, v: str) -> None:
        self.data['selected_model_name'] = v

    @property
    def active_mode(self) -> str:
        return self.data.get('active_mode', 'default')

    @active_mode.setter
    def active_mode(self, v: str) -> None:
        self.data['active_mode'] = v

