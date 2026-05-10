from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


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


def resolve_providers_config_path(path: Optional[str], module_file: str) -> Path:
    if path:
        return Path(path)
    return Path(module_file).parents[2] / "config" / "providers.json"


def set_provider_active_flag(
    *,
    provider_type: str,
    active: bool,
    resolve_config_path: Callable[[Optional[str]], Path],
    canonical_provider: Callable[[Optional[str]], str],
    lock: object,
    logger: object,
    traceback_formatter: Callable[[], str] = traceback.format_exc,
) -> None:
    cfg_path = resolve_config_path(None)
    target_key = canonical_provider(provider_type)
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
    lock: object,
) -> Optional[str]:
    try:
        cfg_path = resolve_config_path(providers_config_path)
        with lock:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        providers: list[Any] = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            if provider.get("active") is True:
                key = canonical_provider(provider.get("name") or provider.get("type") or "")
                if key:
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
        if "lm" in provider_type or canonical_provider(provider.get("name")) == "lm_studio":
            try:
                full = lmstudio_full_id(model_str)
                out.append(full if valid_str(full) else model_str)
            except Exception:
                out.append(model_str)
        else:
            out.append(model_str)
    return out
