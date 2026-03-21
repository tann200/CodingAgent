"""F10: Dynamic token budget helper.

Reads the active provider's `context_length` from providers.json and returns
a token budget appropriate for context-building (a configurable fraction of
the total context window), clamped to sane min/max values.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_CONTEXT_LENGTH = 32768
_PROVIDERS_SEARCH_PATHS = [
    Path(__file__).parent.parent.parent / "config" / "providers.json",
    Path(__file__).parent.parent.parent.parent / "config" / "providers.json",
]


def _load_active_context_length() -> int:
    """Return the context_length of the active provider, or a sensible default."""
    for path in _PROVIDERS_SEARCH_PATHS:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # providers.json is an array; find the first active provider
                if isinstance(data, list):
                    for provider in data:
                        if isinstance(provider, dict) and provider.get("active"):
                            ctx = provider.get("context_length")
                            if isinstance(ctx, int) and ctx > 0:
                                logger.debug(
                                    f"provider_context: active provider "
                                    f"'{provider.get('name')}' context_length={ctx}"
                                )
                                return ctx
                    # No explicit active flag — use first entry
                    if data and isinstance(data[0], dict):
                        ctx = data[0].get("context_length")
                        if isinstance(ctx, int) and ctx > 0:
                            return ctx
            except Exception as exc:
                logger.debug(f"provider_context: failed to load {path}: {exc}")
    return _DEFAULT_CONTEXT_LENGTH


def get_context_budget(
    fraction: float = 0.65,
    min_tokens: int = 6000,
    max_tokens: int = 32000,
) -> int:
    """
    Return a token budget for context-building based on the active provider's
    context window.

    fraction  — portion of the context window to allocate (default 0.65)
    min_tokens — lower clamp (guarantees a usable minimum even for tiny models)
    max_tokens — upper clamp (avoids bloated prompts for very large context windows)
    """
    ctx_len = _load_active_context_length()
    budget = int(ctx_len * fraction)
    return max(min_tokens, min(budget, max_tokens))
