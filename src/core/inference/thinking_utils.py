"""
Utilities for handling LLM thinking/reasoning tokens.

Some models (Qwen3, DeepSeek-R1-Distill) emit <think>...</think> blocks
before their actual response.  These utilities let the codebase:

  1. Detect whether the active model is a reasoning model.
  2. Strip <think> blocks from any response string.
  3. Choose an appropriate max_tokens budget (reasoning models need a larger
     budget so thinking tokens don't crowd out the real answer).
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# Substrings that identify models with automatic thinking-token generation.
# Qwen3 supports /no_think; DeepSeek-R1-Distill does not — both need a larger
# token budget when thinking cannot be disabled.
_REASONING_MODEL_PATTERNS = (
    "qwen3",
    "deepseek-r1",
    "deepseek_r1",
    "qwq",
)

# Qwen3 specifically supports /no_think to suppress the think block entirely.
_NO_THINK_SUPPORTED_PATTERNS = (
    "qwen3",
    "qwq",
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def is_reasoning_model(model_id: str) -> bool:
    """Return True when *model_id* is known to emit <think> blocks by default."""
    lowered = (model_id or "").lower()
    return any(pat in lowered for pat in _REASONING_MODEL_PATTERNS)


def supports_no_think(model_id: str) -> bool:
    """Return True when *model_id* honours the /no_think prompt directive."""
    lowered = (model_id or "").lower()
    return any(pat in lowered for pat in _NO_THINK_SUPPORTED_PATTERNS)


def strip_thinking(text: str) -> str:
    """Remove all <think>...</think> blocks from *text*.

    Safe to call on any model's output — non-thinking models produce no such
    blocks so the string is returned unchanged.
    """
    return _THINK_RE.sub("", text).strip()


def budget_max_tokens(base: int, model_id: str) -> int:
    """Return an adjusted max_tokens budget for *model_id*.

    For reasoning models that cannot suppress thinking tokens (e.g.
    DeepSeek-R1-Distill), the thinking block may consume most of the token
    budget before the real answer starts.  We double the allocation so the
    final JSON/text output is not truncated.

    For models where /no_think works (Qwen3), the base budget is sufficient.
    For all other models there is no overhead, so the base is returned as-is.
    """
    if is_reasoning_model(model_id) and not supports_no_think(model_id):
        adjusted = base * 2
        logger.debug(
            f"thinking_utils: doubling max_tokens {base} → {adjusted} for reasoning model '{model_id}'"
        )
        return adjusted
    return base


def get_active_model_id() -> str:
    """Best-effort lookup of the currently configured model ID.

    Returns an empty string if the model cannot be determined (e.g. during
    testing or before the provider is initialised).
    """
    try:
        from src.core.inference.llm_manager import load_provider

        raw = load_provider(None)
        providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        if providers:
            p = providers[0]
            # providers.json entries may carry a 'model' or 'default_model' key
            model_id = p.get("model") or p.get("default_model") or p.get("name") or ""
            return str(model_id)
    except Exception:
        pass
    return ""
