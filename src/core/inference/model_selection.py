from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional
from pathlib import Path
import json
import os
import shutil
import tempfile
import traceback


def select_model_name(models: List[Any], requested: Optional[str]) -> Optional[str]:
    if not models:
        return None

    names: List[str] = []
    for model in models:
        if isinstance(model, dict):
            model_id = model.get("id") or model.get("key") or model.get("name")
            if model_id:
                names.append(str(model_id))
        elif isinstance(model, str):
            names.append(model)

    if requested:
        if requested in names:
            return requested
        for name in names:
            if name.endswith("/" + requested) or name.split("/")[-1] == requested:
                return name
        return None

    return names[0] if names else None


def lmstudio_full_id(raw: str) -> str:
    """Return a canonical LM Studio full id for a model string."""
    if not raw:
        return raw

    value = str(raw)
    if "/" in value:
        return value
    if ":" in value:
        left, right = value.split(":", 1)
        match = re.match(r"^([a-zA-Z]+)", left)
        vendor = match.group(1) if match else left
        return f"{vendor}/{left}-{right}"
    return value


def canonical_provider(
    name: Optional[str],
    *,
    canonical_provider_name_fn: Callable[[Optional[str]], str],
) -> str:
    return canonical_provider_name_fn(name)


def normalize_models_for_provider(
    provider: Dict[str, Any],
    *,
    normalize_provider_models_fn: Callable[..., List[str]],
    valid_str_fn: Callable[[Any], bool],
    canonical_provider_fn: Callable[[Optional[str]], str],
    lmstudio_full_id_fn: Callable[[str], str],
) -> List[str]:
    return normalize_provider_models_fn(
        provider,
        valid_str=valid_str_fn,
        canonical_provider=canonical_provider_fn,
        lmstudio_full_id=lmstudio_full_id_fn,
    )


def resolve_config_path(
    path: Optional[str],
    *,
    resolve_providers_config_path_fn: Callable[[Optional[str], str], Path],
    current_file: str,
) -> Path:
    return resolve_providers_config_path_fn(path, current_file)


def set_provider_active(
    *,
    provider_type: str,
    active: bool,
    set_provider_active_flag_fn: Callable[..., None],
    resolve_config_path_fn: Callable[[Optional[str]], Path],
    canonical_provider_fn: Callable[[Optional[str]], str],
    lock: Any,
    logger: Any,
) -> None:
    set_provider_active_flag_fn(
        provider_type=provider_type,
        active=active,
        resolve_config_path=resolve_config_path_fn,
        canonical_provider=canonical_provider_fn,
        lock=lock,
        logger=logger,
    )


def load_provider(
    path: Optional[str],
    *,
    resolve_config_path_fn: Callable[[Optional[str]], Path],
    open_fn: Callable[..., Any] = open,
) -> Any:
    try:
        resolved_path = resolve_config_path_fn(path)
        text = None
        try:
            text = Path(resolved_path).read_text(encoding="utf-8")
        except Exception:
            try:
                with open_fn(resolved_path, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except Exception:
                return None
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None
    except Exception:
        return None


def save_provider(
    data: Any,
    *,
    path: Optional[str],
    initial_path: Optional[Path],
    resolve_config_path_fn: Callable[[Optional[str]], Path],
    logger: Any,
    atomic_write_json_importer: Callable[[], Callable[..., bool]] | None = None,
) -> bool:
    try:
        target = None
        if initial_path:
            try:
                target = Path(initial_path)
            except Exception:
                target = None
        if target is None:
            target = resolve_config_path_fn(path)

        target.parent.mkdir(parents=True, exist_ok=True)
        to_write = data
        if isinstance(data, dict) and target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    name = data.get("name")
                    updated = [
                        item
                        if (not isinstance(item, dict) or item.get("name") != name)
                        else data
                        for item in existing
                    ]
                    if not any(
                        isinstance(item, dict) and item.get("name") == name
                        for item in existing
                    ):
                        updated.append(data)
                    to_write = updated
            except Exception:
                pass

        try:
            if atomic_write_json_importer is None:
                from src.core.io_utils import atomic_write_json as atomic_write_json_fn
            else:
                atomic_write_json_fn = atomic_write_json_importer()

            logger.debug("llm_manager: attempting atomic_write_json for %s", target)
            ok = atomic_write_json_fn(target, to_write, logger=logger)
            if ok:
                logger.debug("llm_manager: atomic_write_json succeeded for %s", target)
                return True
            logger.warning(
                "llm_manager: atomic_write_json returned False for %s; falling back to write_text",
                target,
            )
        except Exception:
            logger.debug(
                "llm_manager: atomic_write_json unavailable or failed for %s; falling back\n%s",
                target,
                traceback.format_exc(),
            )

        try:
            fd = None
            tmp = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = None
                    json.dump(to_write, handle, ensure_ascii=False)
                    try:
                        handle.flush()
                        os.fsync(handle.fileno())
                    except Exception:
                        pass
                try:
                    os.replace(tmp, str(target))
                except Exception:
                    try:
                        shutil.move(tmp, str(target))
                    except Exception:
                        pass
            finally:
                try:
                    if fd is not None:
                        os.close(fd)
                except Exception:
                    pass
            return True
        except Exception:
            try:
                target.write_text(json.dumps(to_write), encoding="utf-8")
                return True
            except Exception:
                logger.debug(
                    "llm_manager: fallback write failed for %s\n%s",
                    target,
                    traceback.format_exc(),
                )
                return False
    except Exception:
        return False


def resolve_requested_model(
    models: List[Any],
    requested: Optional[str],
    *,
    select_model_name_fn: Callable[[List[Any], Optional[str]], Optional[str]],
    event_bus: Any = None,
    provider_key: Optional[str] = None,
) -> Optional[str]:
    if not models:
        return None
    try:
        selected = select_model_name_fn(models, requested)
        if selected:
            return selected
        try:
            if event_bus:
                event_bus.publish(
                    "provider.model.missing",
                    {
                        "provider": provider_key,
                        "requested": requested,
                        "available": models,
                    },
                )
        except Exception:
            pass
    except Exception:
        return None
    return None
