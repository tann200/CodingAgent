"""credentials.py — Secure API key storage.

TASK-04: Provides ``get_api_key()`` and ``set_api_key()`` that use the system
keychain via ``keyring`` when available, falling back to ``prefs.json`` with
a warning.  This replaces direct prefs.json API key reads/writes scattered
across the codebase.

Usage::

    from src.core.credentials import get_api_key, set_api_key

    key = get_api_key("openai")
    set_api_key("openai", "sk-...")

Security properties:
- ``keyring`` stores credentials in the OS keychain (macOS Keychain, Windows
  Credential Manager, libsecret on Linux).  Keys never touch the filesystem.
- Fallback to ``prefs.json`` warns the user that the storage is not as secure.
- ``delete_api_key()`` removes from both stores for clean rotation.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import traceback
from typing import Optional

from src.core.paths import get_prefs_path

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "codingagent"
_PREFS_PATH = get_prefs_path()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _keyring_get(provider: str) -> Optional[str]:
    """Try to retrieve key from keyring; return None on any failure."""
    try:
        import keyring  # optional dependency  # type: ignore[import]

        value = keyring.get_password(_KEYRING_SERVICE, provider)
        return value or None
    except Exception:
        return None


def _keyring_set(provider: str, key: str) -> bool:
    """Try to store key in keyring; return True on success."""
    try:
        import keyring  # type: ignore[import]

        keyring.set_password(_KEYRING_SERVICE, provider, key)
        return True
    except Exception:
        return False


def _keyring_delete(provider: str) -> bool:
    """Try to delete key from keyring; return True on success."""
    try:
        import keyring  # type: ignore[import]

        keyring.delete_password(_KEYRING_SERVICE, provider)
        return True
    except Exception:
        return False


def _prefs_get(provider: str) -> Optional[str]:
    """Retrieve API key from prefs.json fallback store."""
    if not _PREFS_PATH.exists():
        return None
    try:
        data = json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
        return data.get("api_keys", {}).get(provider) or None
    except Exception:
        return None


def _write_prefs_data(data: dict, label: str) -> None:
    """Write *data* to ``_PREFS_PATH`` atomically with restricted permissions.

    Tries ``atomic_write_json`` first; falls back to mkstemp+replace.
    """
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        from src.core.io_utils import atomic_write_json

        logger.debug("credentials: attempting atomic_write_json for %s (%s)", _PREFS_PATH, label)
        ok = atomic_write_json(_PREFS_PATH, data, logger=logger)
        if ok:
            try:
                os.chmod(_PREFS_PATH, 0o600)
            except Exception:
                pass
            logger.debug("credentials: atomic_write_json succeeded for %s (%s)", _PREFS_PATH, label)
            return
        logger.warning(
            "credentials: atomic_write_json returned False for %s (%s); falling back",
            _PREFS_PATH,
            label,
        )
    except Exception:
        logger.debug(
            "credentials: atomic_write_json unavailable or failed for %s (%s); falling back\n%s",
            _PREFS_PATH,
            label,
            traceback.format_exc(),
        )

    # Fallback: legacy mkstemp + replace flow.
    fd, tmp = tempfile.mkstemp(dir=_PREFS_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, _PREFS_PATH)
        try:
            os.chmod(_PREFS_PATH, 0o600)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _prefs_set(provider: str, key: str) -> None:
    """Write API key to prefs.json fallback store."""
    try:
        data: dict = {}
        if _PREFS_PATH.exists():
            try:
                data = json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        data.setdefault("api_keys", {})[provider] = key
        _write_prefs_data(data, "set")
    except Exception as exc:
        logger.warning("credentials: failed to write prefs.json: %s", exc)


def _prefs_delete(provider: str) -> None:
    """Remove API key from prefs.json."""
    if not _PREFS_PATH.exists():
        return
    try:
        data = json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
        data.get("api_keys", {}).pop(provider, None)
        _write_prefs_data(data, "delete")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_api_key(provider: str) -> Optional[str]:
    """Return the stored API key for *provider*, or None if not found.

    Checks the OS keychain first; falls back to ``prefs.json``.

    Args:
        provider: Provider name, e.g. ``"openai"``, ``"anthropic"``,
                  ``"openrouter"``.
    """
    # 1. Keychain
    value = _keyring_get(provider)
    if value:
        return value

    # 2. prefs.json fallback
    value = _prefs_get(provider)
    if value:
        return value

    return None


def set_api_key(provider: str, key: str) -> None:
    """Store *key* for *provider*.

    Attempts to store in the OS keychain first.  If keyring is unavailable,
    falls back to ``prefs.json`` with a warning.

    Args:
        provider: Provider name, e.g. ``"openai"``.
        key:      The API key string to store.
    """
    if not key:
        logger.warning("credentials: refusing to store empty API key for %r", provider)
        return

    # 1. Try keychain
    if _keyring_set(provider, key):
        logger.debug("credentials: stored key for %r in system keychain", provider)
        return

    # 2. Fallback to prefs.json
    logger.warning(
        "credentials: keyring unavailable — storing key for %r in prefs.json (less secure). "
        "Install the 'keyring' package for secure storage.",
        provider,
    )
    _prefs_set(provider, key)


def delete_api_key(provider: str) -> None:
    """Remove the stored API key for *provider* from all stores.

    Args:
        provider: Provider name, e.g. ``"openai"``.
    """
    _keyring_delete(provider)
    _prefs_delete(provider)
    logger.debug("credentials: deleted key for %r from all stores", provider)
