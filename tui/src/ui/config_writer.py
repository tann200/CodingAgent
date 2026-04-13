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
from typing import Optional

try:
    from src.ui.logging import get_logger
except ImportError:
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
    """Atomically persist API key (and optional base_url) for a provider.

    The credentials are merged into the matching entry inside the top-level
    list in providers.json.  If no entry matches *provider_id* a new minimal
    entry is appended so credentials are never silently lost.
    """
    providers = _load()

    # Find the matching entry (mutate in-place)
    entry = next(
        (
            p
            for p in providers
            if isinstance(p, dict) and _provider_matches(p, provider_id)
        ),
        None,
    )
    if entry is None:
        # No existing entry — append a minimal placeholder so the key is saved
        entry = {"name": provider_id, "type": provider_id}
        providers.append(entry)
        logger.warning(
            f"save_provider_credentials: no entry for '{provider_id}' found in "
            f"providers.json — appending new entry"
        )

    if api_key:
        entry["api_key"] = api_key
    if base_url:
        entry["base_url"] = base_url

    _atomic_write(providers)


def load_provider_credentials(provider_id: str) -> dict:
    """Return stored credentials for provider_id, or empty dict."""
    for entry in _load():
        if isinstance(entry, dict) and _provider_matches(entry, provider_id):
            return {k: entry[k] for k in ("api_key", "base_url") if k in entry}
    return {}


def load_all_credentials() -> dict[str, dict]:
    """Return all stored provider credentials keyed by normalised provider id."""
    result: dict[str, dict] = {}
    for entry in _load():
        if not isinstance(entry, dict):
            continue
        pid = (entry.get("name") or entry.get("type") or "").lower().replace(" ", "_")
        if pid:
            creds = {k: entry[k] for k in ("api_key", "base_url") if k in entry}
            if creds:
                result[pid] = creds
    return result
