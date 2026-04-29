"""
Utilities for handling LLM thinking/reasoning tokens.

Some models (Qwen3, DeepSeek-R1-Distill, OpenAI o-series) use a
"reasoning-first" pattern before their actual response.  These utilities let
the codebase:

  1. Detect whether the active model is a reasoning model.
  2. Strip <think> blocks from any response string.
  3. Choose an appropriate max_tokens budget (reasoning models need a larger
     budget so thinking tokens don't crowd out the real answer).
  4. Control thinking mode via CLI (--thinking auto|on|off)
"""

from __future__ import annotations

import re
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class ThinkingMode(str, Enum):
    OFF = "off"  # Never use thinking (small local models default)
    AUTO = "auto"  # Enabled for reasoning models or multi-step tasks
    ON = "on"  # Always on (debug, complex tasks)


# Substrings that identify models with automatic thinking-token generation or
# "reasoning-first" behaviour that requires the beast/reasoning prompt variant.
#
# Local reasoning models (Qwen3, DeepSeek-R1) emit <think> blocks.
# OpenAI o-series (o1, o3, o4) use server-side chain-of-thought; they do NOT
# emit <think> blocks but still need a different prompting style (no CoT
# instructions, direct output, no streaming tool-call format).
_REASONING_MODEL_PATTERNS = (
    "qwen3",  # Covers qwen3, qwen3.5, qwen3.5-9b
    "deepseek-r1",
    "deepseek_r1",
    "qwq",
    # OpenAI o-series reasoning models
    "o1-",
    "o1-mini",
    "o1-preview",
    "o3-mini",
    "o3-",
    "o4-",
)

# Qwen3 specifically supports /no_think to suppress the think block entirely.
_NO_THINK_SUPPORTED_PATTERNS = (
    "qwen3",  # Covers qwen3, qwen3.5
    "qwq",
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def is_reasoning_model(model_id: str) -> bool:
    """Return True when *model_id* is known to emit ＜think＞ blocks by default."""
    # BUG-FIX: handle non-string input gracefully
    lowered = str(model_id).lower() if model_id is not None else ""
    return any(pat in lowered for pat in _REASONING_MODEL_PATTERNS)


def supports_no_think(model_id: str) -> bool:
    """Return True when *model_id* honours the /no_think prompt directive."""
    # BUG-FIX: handle non-string input gracefully
    lowered = str(model_id).lower() if model_id is not None else ""
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
        providers = (
            raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        )
        if providers:
            p = providers[0]
            model_id = p.get("model") or p.get("default_model") or p.get("name") or ""
            return str(model_id)
    except Exception:
        pass
    return ""


def resolve_thinking_mode(cli_mode: ThinkingMode | None, model_id: str) -> bool:
    """Resolve whether thinking should be enabled for this model.

    Args:
        cli_mode: CLI --thinking flag value (None = use default)
        model_id: Active model identifier

    Returns:
        True if thinking should be enabled, False otherwise
    """
    if cli_mode == ThinkingMode.ON:
        return True
    if cli_mode == ThinkingMode.OFF:
        return False

    # AUTO mode: enable for reasoning models
    if cli_mode == ThinkingMode.AUTO or cli_mode is None:
        return is_reasoning_model(model_id)

    return False


def get_thinking_directive(model_id: str, enabled: bool) -> str | None:
    """Get the prompt directive to control thinking mode.

    Args:
        model_id: Active model identifier
        enabled: Whether thinking should be enabled

    Returns:
        Prompt directive string, or None if model doesn't support it
    """
    if not enabled and supports_no_think(model_id):
        return "/no_think"
    if enabled and model_id.lower().startswith("qwen3"):
        return None  # Qwen3 thinks by default, no directive needed
    return None
