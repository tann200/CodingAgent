import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
import os
from src.core.paths import get_prefs_path

DEFAULT_FILENAME = os.getenv("CODINGAGENT_PREFS") or str(get_prefs_path())

logger = logging.getLogger(__name__)


class UserPrefs:
    def __init__(
        self, data: Optional[Dict[str, Any]] = None, path: Optional[Path] = None
    ):
        self.path = Path(path) if path else Path(DEFAULT_FILENAME)
        self.data = data or {}
        # CODE_QUALITY_AUDIT #8 fix: the three lines below previously assigned
        # instance attributes that shadow the @property descriptors defined later
        # in this class.  Since the properties already read from self.data, the
        # assignments caused a double-write on every construction (setter invoked
        # from __init__, then the property re-reads the same value from self.data).
        # Removing the redundant assignments lets the @property descriptors handle
        # all reads/writes cleanly; self.data is the single source of truth.
        # Defaults that were previously set here are now handled by the property
        # getters (which return self.data.get(..., default)).

    @classmethod
    def load(cls, path: Optional[str] = None) -> "UserPrefs":
        p = Path(path) if path else Path(DEFAULT_FILENAME)
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                d = {}
        else:
            d = {}
        return cls(data=d, path=p)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                from src.core.io_utils import atomic_write_json

                logger.debug(
                    "user_prefs: attempting atomic_write_json for %s", self.path
                )
                ok = atomic_write_json(self.path, self.data, logger=logger)
                if ok:
                    try:
                        os.chmod(self.path, 0o600)
                    except Exception:
                        # Best-effort: do not fail the save if chmod fails.
                        pass
                    logger.debug(
                        "user_prefs: atomic_write_json succeeded for %s", self.path
                    )
                    return
            except Exception:
                # Fall back to original write_text behaviour
                import traceback

                logger.debug(
                    "user_prefs: atomic_write_json unavailable or error for %s; falling back\n%s",
                    self.path,
                    traceback.format_exc(),
                )
                pass
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                # Best-effort
                pass
        except Exception:
            import traceback

            logger.debug(
                "user_prefs: failed to save prefs to %s\n%s",
                self.path,
                traceback.format_exc(),
            )

    def get_provider_setting(self, provider_name: str, key: str) -> Optional[Any]:
        providers = self.data.get("providers", {})
        p = providers.get(provider_name.lower(), {})
        return p.get(key)

    def set_provider_setting(self, provider_name: str, key: str, value: Any) -> None:
        providers = self.data.setdefault("providers", {})
        p = providers.setdefault(provider_name.lower(), {})
        p[key] = value
        # also update attributes
        if key == "api_key":
            self.save()

    def has_any_api_keys(self) -> bool:
        providers = self.data.get("providers", {})
        for p in providers.values():
            if p.get("api_key"):
                return True
        return False

    def get_provider_key(self, name: str) -> Optional[str]:
        return self.get_provider_setting(name, "api_key")

    def get_mode_model(self, mode: str) -> Optional[str]:
        modes = self.data.get("modes", {})
        return modes.get(mode)

    def set_mode_model(self, mode: str, model: str) -> None:
        modes = self.data.setdefault("modes", {})
        modes[mode] = model
        self.save()

    def update_provider_config(self, provider_key: str, **kwargs) -> None:
        providers = self.data.setdefault("providers", {})
        p = providers.setdefault(provider_key, {})
        for k, v in kwargs.items():
            p[k] = v
        self.save()

    # legacy compatibility
    @property
    def selected_model_provider(self) -> Optional[str]:
        return self.data.get("selected_model_provider")

    @selected_model_provider.setter
    def selected_model_provider(self, v: Optional[str]) -> None:
        self.data["selected_model_provider"] = v

    @property
    def selected_model_name(self) -> Optional[str]:
        return self.data.get("selected_model_name")

    @selected_model_name.setter
    def selected_model_name(self, v: Optional[str]) -> None:
        self.data["selected_model_name"] = v

    @property
    def active_mode(self) -> str:
        return self.data.get("active_mode", "default")

    @active_mode.setter
    def active_mode(self, v: str) -> None:
        self.data["active_mode"] = v
