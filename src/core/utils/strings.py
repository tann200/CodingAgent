"""Small string helpers used for provider/model resolution.

These helpers centralise the pragmatic heuristics used across the codebase to
decide whether a candidate value is a "concrete" provider/model string and to
extract plausible names from dict-like shapes.  Keeping the heuristics in one
place makes future policy changes easier and avoids duplicated logic.
"""

from __future__ import annotations

from typing import Any, Optional


def valid_str(x: Any) -> bool:
    """Return True for concrete, non-empty strings that are not MagicMock placeholders."""
    if not isinstance(x, str):
        return False
    s = x.strip()
    return bool(s) and "MagicMock" not in s


def extract_str(candidate: Any) -> Optional[str]:
    """Extract a concrete string from various candidate types.

    Accepts dicts (checking common keys) and plain strings. Returns None when
    no concrete string is found.
    """
    if candidate is None:
        return None
    if isinstance(candidate, dict):
        for key in (
            "provider_name",
            "name",
            "id",
            "key",
            "model",
            "default_model",
            "type",
        ):
            val = candidate.get(key)
            if valid_str(val):
                return str(val).strip()
        return None
    if isinstance(candidate, str) and valid_str(candidate):
        return candidate.strip()
    return None
