"""
Atomic read/write for src/config/providers.json (§13).
Stores user-configured API keys and base URLs per provider.
Writes via tempfile + os.replace so the file is never in a half-written state.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from src.ui.logging import get_logger

logger = get_logger("config_writer")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "providers.json"


def _load() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"providers.json read error: {exc}")
    return {"providers": {}}


def _atomic_write(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(CONFIG_PATH))
        logger.info(f"providers.json written atomically ({CONFIG_PATH})")
    except Exception as exc:
        logger.error(f"providers.json write error: {exc}")
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def save_provider_credentials(
    provider_id: str,
    api_key: str,
    base_url: Optional[str] = None,
) -> None:
    """Atomically persist API key (and optional base_url) for a provider."""
    data = _load()
    providers: dict = data.setdefault("providers", {})
    entry: dict = providers.setdefault(provider_id, {})
    if api_key:
        entry["api_key"] = api_key
    if base_url:
        entry["base_url"] = base_url
    _atomic_write(data)


def load_provider_credentials(provider_id: str) -> dict:
    """Return stored credentials for provider_id, or empty dict."""
    data = _load()
    return data.get("providers", {}).get(provider_id, {})


def load_all_credentials() -> dict[str, dict]:
    """Return all stored provider credentials."""
    return _load().get("providers", {})
