from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from src.core.inference._protocols import LockProtocol, LoggerProtocol


def canonical_provider_name(name: Optional[str]) -> str:
    if not name:
        return ""
    normalized = str(name).strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in {"lm", "lm_studio", "lmstudio"}:
        return "lm_studio"
    if normalized in {
        "copilot",
        "github_copilot",
        "github-copilot",
        "ghcopilot",
        "github copilot",
    }:
        return "github_copilot"
    return normalized


_BUNDLED_PROVIDERS_CONFIG = (
    Path(__file__).resolve().parents[2] / "config" / "providers.json"
).resolve()


def is_bundled_providers_config(path: Path) -> bool:
    """Return True if *path* points to the tracked bundled ``src/config/providers.json``."""
    try:
        return path.resolve() == _BUNDLED_PROVIDERS_CONFIG
    except Exception:
        return False


def resolve_providers_config_path(path: Optional[str], module_file: str) -> Path:
    if path:
        return Path(path)
    return Path(module_file).parents[2] / "config" / "providers.json"


def apply_user_prefs_to_providers(
    providers: list[dict],
    canonical_provider: Callable[[Optional[str]], str],
) -> list[dict]:
    """Layer user-scoped ``UserPrefs`` overrides over bundled provider defaults in memory.

    Never mutates ``src/config/providers.json`` on disk.
    """
    try:
        from src.core.user_prefs import UserPrefs

        prefs = UserPrefs.load()
    except Exception:
        return providers

    selected_provider = canonical_provider(prefs.selected_model_provider)
    selected_model = prefs.selected_model_name
    provider_overrides = prefs.data.get("providers")
    if not isinstance(provider_overrides, dict):
        provider_overrides = {}

    layered: list[dict] = []
    for entry in providers:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        pkey = canonical_provider(item.get("name") or item.get("type") or "")
        if selected_provider:
            item["active"] = pkey == selected_provider
        if pkey:
            ov = provider_overrides.get(pkey)
            if not isinstance(ov, dict):
                # Also allow raw lowercase keys stored by legacy callers
                ov = provider_overrides.get(str(item.get("type") or "").lower())
            if isinstance(ov, dict):
                if ov.get("base_url"):
                    item["base_url"] = ov["base_url"]
                if not selected_provider and "active" in ov and isinstance(ov["active"], bool):
                    item["active"] = ov["active"]
        if selected_provider and pkey == selected_provider and selected_model:
            item["default_model"] = selected_model
        layered.append(item)
    return layered


def set_provider_active_flag(
    *,
    provider_type: str,
    active: bool,
    resolve_config_path: Callable[[Optional[str]], Path],
    canonical_provider: Callable[[Optional[str]], str],
    lock: LockProtocol,
    logger: LoggerProtocol,
    traceback_formatter: Callable[[], str] = traceback.format_exc,
) -> None:
    cfg_path = resolve_config_path(None)
    target_key = canonical_provider(provider_type)
    if is_bundled_providers_config(cfg_path):
        # TASK-21: Keep tracked src/config/providers.json read-only; persist
        # runtime active-provider state in UserPrefs instead.
        with lock:
            from src.core.user_prefs import UserPrefs

            prefs = UserPrefs.load()
            prefs.update_provider_config(target_key, active=bool(active))
            if active:
                prefs.selected_model_provider = target_key
            elif canonical_provider(prefs.selected_model_provider) == target_key:
                prefs.selected_model_provider = None
            prefs.save()
        return
    with lock:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        providers = raw if isinstance(raw, list) else [raw]
        for provider in providers:
            provider_key = canonical_provider(
                provider.get("type") or provider.get("name") or ""
            )
            if provider_key == target_key:
                provider["active"] = active
                break

        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            from src.core.io_utils import atomic_write_json

            logger.debug("llm_manager: attempting atomic_write_json for %s", cfg_path)
            ok = atomic_write_json(cfg_path, providers, logger=logger)
            if ok:
                logger.debug("llm_manager: atomic_write_json succeeded for %s", cfg_path)
                return
            logger.warning(
                "llm_manager: atomic_write_json returned False for %s; falling back",
                cfg_path,
            )
        except Exception:
            logger.debug(
                "llm_manager: atomic_write_json unavailable or failed for %s; falling back\n%s",
                cfg_path,
                traceback_formatter(),
            )

        fd = None
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(cfg_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = None
                    json.dump(providers, handle, indent=2)
                    try:
                        handle.flush()
                        os.fsync(handle.fileno())
                    except Exception:
                        pass
                try:
                    os.replace(tmp, str(cfg_path))
                except Exception:
                    shutil.move(tmp, str(cfg_path))
            except Exception:
                try:
                    if fd is not None:
                        os.close(fd)
                except Exception:
                    pass
                raise
        except Exception:
            try:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
            raise


def get_active_provider_name(
    *,
    providers_config_path: Optional[str],
    resolve_config_path: Callable[[Optional[str]], Path],
    canonical_provider: Callable[[Optional[str]], str],
    lock: LockProtocol,
) -> Optional[str]:
    try:
        cfg_path = resolve_config_path(providers_config_path)
        bundled = is_bundled_providers_config(cfg_path)
        provider_overrides: dict[str, Any] = {}
        if bundled:
            try:
                from src.core.user_prefs import UserPrefs

                prefs = UserPrefs.load()
                selected = canonical_provider(prefs.selected_model_provider)
                if selected:
                    return selected
                raw_ov = prefs.data.get("providers")
                if isinstance(raw_ov, dict):
                    provider_overrides = raw_ov
            except Exception:
                pass
        with lock:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        providers: list[Any] = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            key = canonical_provider(provider.get("name") or provider.get("type") or "")
            if bundled and key:
                ov = provider_overrides.get(key)
                if isinstance(ov, dict) and ov.get("active") is False:
                    continue
                if isinstance(ov, dict) and ov.get("active") is True:
                    return key
            if provider.get("active") is True and key:
                return key
    except Exception:
        pass
    return None


def normalize_provider_models(
    provider: Mapping[str, object],
    *,
    valid_str: Callable[[object], bool],
    canonical_provider: Callable[[Optional[str]], str],
    lmstudio_full_id: Callable[[str], str],
) -> list[str]:
    out: list[str] = []
    if not provider or not isinstance(provider, dict):
        return out

    provider_type = str(provider.get("type") or "").lower()
    models_field = provider.get("models") or []
    if not isinstance(models_field, list):
        return out

    for model in models_field:
        if isinstance(model, dict):
            model_id = model.get("id") or model.get("key") or model.get("name") or model.get("model")
        elif isinstance(model, str):
            model_id = model
        else:
            continue

        if not model_id or not valid_str(model_id):
            continue

        model_str = str(model_id).strip()
        if "lm" in provider_type or canonical_provider(str(provider.get("name") or "")) == "lm_studio":
            try:
                full = lmstudio_full_id(model_str)
                out.append(full if valid_str(full) else model_str)
            except Exception:
                out.append(model_str)
        else:
            out.append(model_str)
    return out
