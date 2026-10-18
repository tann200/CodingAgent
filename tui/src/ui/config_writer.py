"""
Atomic read/write for src/config/providers.json (§13).
Stores user-configured API keys and base URLs per provider.
Writes via tempfile + os.replace so the file is never in a half-written state.

providers.json format
---------------------
The file is a **top-level list** of provider dicts, e.g.::

    [
      {"name": "lm_studio", "type": "lm_studio", "base_url": "...", ...},
      {"name": "GitHub Copilot", "type": "github_copilot", ...}
    ]

Credentials (api_key, base_url) are merged directly into each provider's dict
entry.  Provider lookup is done by matching ``provider_id`` against
``name.lower().replace(" ", "_")`` **or** ``type`` (both normalised).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

try:
    from tui.tui_src.ui.logging import get_logger
except Exception:
    try:
        from .logging import get_logger
    except Exception:
        import logging

        def get_logger(name: str) -> logging.Logger:
            return logging.getLogger(name)


logger = get_logger("config_writer")

# __file__ = tui/src/ui/config_writer.py → parents[3] = project root
CONFIG_PATH = Path(__file__).resolve().parents[3] / "src" / "config" / "providers.json"


def _load() -> list:
    """Return providers.json contents as a list of dicts.

    Handles both the canonical top-level-list format and the legacy
    ``{"providers": [...]}`` wrapper.  Returns an empty list on any error.
    """
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict):
                inner = raw.get("providers", [])
                # Legacy dict-of-dicts: {"providers": {"lm_studio": {...}}}
                if isinstance(inner, dict):
                    return list(inner.values())
                if isinstance(inner, list):
                    return inner
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"providers.json read error: {exc}")
    return []


def _provider_matches(entry: dict, provider_id: str) -> bool:
    """Return True if *entry* corresponds to *provider_id*.

    Matching rules (all case-insensitive, spaces → underscores):
    - ``entry["name"].lower().replace(" ", "_") == provider_id``
    - ``entry["type"].lower().replace(" ", "_") == provider_id``
    - ``entry["name"].lower() == provider_id``
    """
    pid = provider_id.lower()
    name = (entry.get("name") or "").lower()
    ptype = (entry.get("type") or "").lower()
    return (
        name.replace(" ", "_") == pid
        or ptype.replace(" ", "_") == pid
        or name == pid
        or ptype == pid
    )


def _atomic_write(data: list) -> None:
    # Create parent dir immediately before writing. Prefer the central
    # atomic_write_json helper; if unavailable or it returns False, fall back
    # to the legacy mkstemp+replace path.
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        from src.core.io_utils import atomic_write_json

        logger.debug("config_writer: attempting atomic_write_json for %s", CONFIG_PATH)
        ok = atomic_write_json(CONFIG_PATH, data, logger=logger)
        if ok:
            logger.info("providers.json written atomically (%s)", CONFIG_PATH)
            return
        logger.warning(
            "config_writer: atomic_write_json returned False for %s; falling back",
            CONFIG_PATH,
        )
    except Exception as exc:
        import traceback as _traceback

        logger.debug(
            "config_writer: atomic_write_json unavailable or failed for %s; falling back: %s\n%s",
            CONFIG_PATH,
            exc,
            _traceback.format_exc(),
        )

    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(CONFIG_PATH))
        logger.info("providers.json written atomically (fallback) (%s)", CONFIG_PATH)
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
    """Atomically persist API key (and optional base_url) in user-level prefs.json."""
    try:
        from src.core.user_prefs import UserPrefs

        prefs = UserPrefs.load()
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        else:
            p = prefs.data.get("providers", {}).get(provider_id.lower(), {})
            if "api_key" in p:
                del p["api_key"]
                prefs.save()
        if base_url:
            kwargs["base_url"] = base_url
        if kwargs:
            prefs.update_provider_config(provider_id.lower(), **kwargs)
    except Exception as exc:
        logger.warning(f"save_provider_credentials to UserPrefs failed: {exc}")


def load_provider_credentials(provider_id: str) -> dict:
    """Return stored credentials for provider_id, or empty dict."""
    res: dict[str, Any] = {}
    try:
        from src.core.user_prefs import UserPrefs

        prefs = UserPrefs.load()
        p = prefs.data.get("providers", {}).get(provider_id.lower(), {})
        if p:
            res.update({k: p[k] for k in ("api_key", "base_url") if k in p and p[k]})
    except Exception:
        pass
    for entry in _load():
        if isinstance(entry, dict) and _provider_matches(entry, provider_id):
            for k in ("api_key", "base_url"):
                if k in entry and k not in res:
                    res[k] = entry[k]
            break
    return res


def load_all_credentials() -> dict[str, dict]:
    """Return all stored provider credentials keyed by normalised provider id."""
    result: dict[str, dict] = {}
    try:
        from src.core.user_prefs import UserPrefs

        prefs = UserPrefs.load()
        for pid, p in prefs.data.get("providers", {}).items():
            creds = {k: p[k] for k in ("api_key", "base_url") if k in p and p[k]}
            if creds:
                result[pid.lower()] = creds
    except Exception:
        pass
    for entry in _load():
        if not isinstance(entry, dict):
            continue
        pid = (entry.get("name") or entry.get("type") or "").lower().replace(" ", "_")
        if pid:
            creds = {k: entry[k] for k in ("api_key", "base_url") if k in entry}
            if creds:
                if pid not in result:
                    result[pid] = {}
                for k, v in creds.items():
                    result[pid].setdefault(k, v)
    return result
