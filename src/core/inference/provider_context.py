"""F10: Dynamic token budget helper.

Reads the active provider's `context_length` from providers.json and returns
a token budget appropriate for context-building (a configurable fraction of
the total context window), clamped to sane min/max values.

Also provides a model pricing table (TASK-18) used by TUI-09 to compute
per-run cost estimates.  Prices are in USD per 1 000 tokens — tuple is
(input_price_per_1k, output_price_per_1k).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# TASK-18: Model pricing table — (input $/1k tokens, output $/1k tokens).
# Prices sourced from public provider documentation; update as needed.
_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI (2025-2026 + legacy)
    "gpt-6": (0.005, 0.020),
    "gpt-5": (0.0025, 0.010),
    "gpt-4o": (0.0025, 0.010),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.010, 0.030),
    "gpt-4": (0.030, 0.060),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "o4-mini": (0.0011, 0.0044),
    "o3": (0.002, 0.008),
    "o3-mini": (0.0011, 0.0044),
    "o1": (0.015, 0.060),
    "o1-mini": (0.003, 0.012),
    # Anthropic (2025-2026 + legacy)
    "claude-opus-4-6": (0.015, 0.075),
    "claude-opus-4.6": (0.015, 0.075),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-sonnet-4.6": (0.003, 0.015),
    "claude-sonnet-4-5": (0.003, 0.015),
    "claude-sonnet-4.5": (0.003, 0.015),
    "claude-haiku-4-5": (0.0008, 0.004),
    "claude-3-5-sonnet-20241022": (0.003, 0.015),
    "claude-3-5-haiku-20241022": (0.0008, 0.004),
    "claude-3-opus-20240229": (0.015, 0.075),
    "claude-3-sonnet-20240229": (0.003, 0.015),
    "claude-3-haiku-20240307": (0.00025, 0.00125),
    # Google Gemini & Gemma (2025-2026 + legacy)
    "gemini-3.6-flash": (0.0003, 0.0012),
    "gemini-3.5-flash-lite": (0.000075, 0.0003),
    "gemini-3.5-flash": (0.00025, 0.001),
    "gemini-3.1-pro-preview": (0.00125, 0.005),
    "gemini-3.1-flash-lite": (0.000075, 0.0003),
    "gemini-2.5-pro": (0.00125, 0.005),
    "gemini-2.5-flash": (0.00015, 0.0006),
    "gemma-4-31b-it": (0.0001, 0.0002),
    "gemma-4-26b-a4b-it": (0.00008, 0.00016),
    "gemini-1.5-pro": (0.00125, 0.005),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "gemini-2.0-flash": (0.0001, 0.0004),
    # Groq / Open-weight (2025-2026 + legacy)
    "qwen/qwen3.8-27b": (0.0003, 0.0006),
    "openai/gpt-oss-120b": (0.0005, 0.001),
    "openai/gpt-oss-20b": (0.0001, 0.0002),
    "llama-3.3-70b-versatile": (0.00059, 0.00079),
    # Mistral
    "mistral-large-latest": (0.002, 0.006),
    "mistral-small-latest": (0.0002, 0.0006),
    # Meta / open-weight (OpenRouter pricing)
    "meta-llama/llama-3.1-70b-instruct": (0.0009, 0.0009),
    "meta-llama/llama-3.1-8b-instruct": (0.0001, 0.0001),
    # DeepSeek
    "deepseek/deepseek-chat": (0.00014, 0.00028),
    "deepseek/deepseek-r1": (0.00055, 0.00219),
    # Qwen
    "qwen/qwen-2.5-72b-instruct": (0.0009, 0.0009),
}

# Fallback rate used when the model is not in _PRICING.
_DEFAULT_INPUT_PRICE_PER_1K = 0.001  # $0.001 / 1k input tokens
_DEFAULT_OUTPUT_PRICE_PER_1K = 0.003  # $0.003 / 1k output tokens


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    model: str = "",
) -> float:
    """Estimate the USD cost of a model call.

    Looks up *model* in ``_PRICING``.  If not found, applies a conservative
    fallback rate.  Returns a float rounded to 6 decimal places.

    Args:
        input_tokens:  Number of prompt / input tokens consumed.
        output_tokens: Number of completion / output tokens generated.
        model:         Model identifier string (case-sensitive, matched against
                       ``_PRICING`` keys).

    Returns:
        Estimated cost in USD.
    """
    # Try exact match first, then partial/prefix match for versioned IDs.
    price = _PRICING.get(model)
    if price is None:
        for key, val in _PRICING.items():
            if model.startswith(key) or key.startswith(model):
                price = val
                break
    if price is None:
        price = (_DEFAULT_INPUT_PRICE_PER_1K, _DEFAULT_OUTPUT_PRICE_PER_1K)

    input_price, output_price = price
    cost = (input_tokens / 1000.0) * input_price + (
        output_tokens / 1000.0
    ) * output_price
    return round(cost, 6)


_DEFAULT_CONTEXT_LENGTH = 32768
_PROVIDERS_SEARCH_PATHS = [
    Path(__file__).parent.parent.parent / "config" / "providers.json",
    Path(__file__).parent.parent.parent.parent / "config" / "providers.json",
]

# LIVE-CTX: Runtime override set by the provider.context_window event handler
# when the models endpoint returns the actual loaded context length.  Takes
# precedence over providers.json so that get_context_budget() reflects reality
# rather than a static config value.
_live_context_length: int = 0
_live_context_provider: str = ""
_context_lock = threading.Lock()


def set_active_context_length(length: int, provider_id: str = "") -> None:
    """Override the active context length with a live value from the models endpoint.

    Called by the TUI bridge (and any other subscriber of the
    ``provider.context_window`` event) as soon as the real loaded context length
    is known.  A value of 0 or negative clears the override so the file-based
    fallback is used again.

    Args:
        length:     Context window size in tokens.
        provider_id: Identifier of the provider that reported this length, so
                     callers can detect stale values from a previous provider.
    """
    global _live_context_length, _live_context_provider
    with _context_lock:
        _live_context_length = max(0, int(length))
        _live_context_provider = provider_id
        logger.debug(
            f"provider_context: live context length set to {_live_context_length} "
            f"for provider '{provider_id}'"
        )


def _load_active_context_length(provider_id: str = "") -> int:
    """Return the context_length of the active provider, or a sensible default.

    Prefers the live value set by ``set_active_context_length()`` (populated
    from the models endpoint at runtime) over the static providers.json entry.

    Args:
        provider_id: Expected provider identifier.  If provided and does not
                     match the stored provider, the live override is treated as
                     stale and the file-based fallback is used.
    """
    # LIVE-CTX: prefer runtime value from models endpoint
    with _context_lock:
        if _live_context_length > 0:
            if not provider_id or _live_context_provider == provider_id:
                return _live_context_length
            logger.debug(
                f"provider_context: live context length was set by "
                f"'{_live_context_provider}' but caller expects '{provider_id}'; "
                f"falling back to providers.json"
            )

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


def get_actual_context_window() -> int:
    """Return the raw context window size of the active provider.

    Unlike ``get_context_budget()``, this does NOT apply a fraction or an
    upper cap.  Use this for overflow detection where the goal is to know
    whether the prompt is approaching the hard limit, not the soft budget.

    OP-4: replaces ``get_context_budget()`` in the overflow-detection path of
    perception_node so that 200K-window models correctly detect overflow at
    ~196K tokens instead of the 131K cap that ``get_context_budget`` imposes.
    """
    return _load_active_context_length()


# P3-F: Per-tier context fractions.
# Gemma 4 26B A4B has 256K context - more room for prompt.
# SMALL: 60% (16K of 16K), MEDIUM: 75% (192K of 256K), LARGE: 80%.
_TIER_CONTEXT_FRACTION: dict[str, float] = {
    "small": 0.60,  # 7–14B / 16K ctx
    "medium": 0.75,  # 256K ctx (Gemma 4 26B A4B)
    "large": 0.80,  # 256K+ ctx
    "frontier": 0.80,  # cloud / 200K+ ctx
}


def get_context_budget(
    fraction: float = 0.65,
    min_tokens: int = 6000,
    max_tokens: int = 262144,  # 256K for Gemma 4 26B A4B
    model_tier: str = "",
) -> int:
    """
    Return a token budget for context-building based on the active provider's
    context window.

    Prefers the live context length reported by the models endpoint (set via
    ``set_active_context_length()``) over the static providers.json value.

    P3-F: When *model_tier* is provided, overrides *fraction* with the
    tier-specific value from ``_TIER_CONTEXT_FRACTION`` so that SMALL models
    get a smaller budget than LARGE models that have 256K tokens to work with.

    fraction   — default fraction when model_tier is absent or unknown (0.65)
    min_tokens — lower clamp (guarantees a usable minimum even for tiny models)
    max_tokens — upper clamp (avoids bloated prompts for very large context windows)
    model_tier — optional tier string (e.g. "small", "medium", "frontier")
    """
    if model_tier:
        fraction = _TIER_CONTEXT_FRACTION.get(model_tier.lower(), fraction)
    ctx_len = _load_active_context_length()
    budget = int(ctx_len * fraction)
    return max(min_tokens, min(budget, max_tokens))


# OPT-03: Single-source response reserve and output-cap clamping (modeled on
# localharness's context reserve contract, with a strictly monotonic curve).
RESPONSE_RESERVE_TOKENS: int = 4096
_MIN_PROMPT_FLOOR_TOKENS: int = 1024


def response_reserve(
    max_context_tokens: int,
    max_response_tokens: int | None = None,
) -> int:
    """Return the output-token reserve held back from *max_context_tokens*.

    This is the single canonical planning reserve so prompt builders,
    compactors, and request builders never double-subtract output headroom:

    * Non-positive windows reserve ``0``.
    * Small windows scale monotonically at ``max_context_tokens // 4`` (floored
      at ``256``), capped so at least ``1,024`` tokens survive for the prompt
      when ``max_context_tokens > 1,024``.
    * Windows ``>= 16,384`` reserve at least ``RESPONSE_RESERVE_TOKENS``
      (4,096) and grow to accommodate an explicit ``max_response_tokens`` cap,
      bounded at ``max_context_tokens // 2`` so history always retains at least
      half the window.
    """
    if max_context_tokens <= 0:
        return 0
    prompt_cap = max(0, max_context_tokens - _MIN_PROMPT_FLOOR_TOKENS)
    if prompt_cap == 0:
        return min(max_context_tokens // 4, max_context_tokens)
    base_reserve = min(RESPONSE_RESERVE_TOKENS, max(256, max_context_tokens // 4))
    if max_context_tokens >= 16_384 and max_response_tokens and max_response_tokens > 0:
        wanted = max(base_reserve, int(max_response_tokens))
        return min(wanted, max_context_tokens // 2, prompt_cap)
    return min(base_reserve, prompt_cap)


def clamp_response_tokens(
    max_context_tokens: int,
    configured_max_tokens: int | None,
) -> int | None:
    """Clamp a configured per-request ``max_tokens`` so ``prompt + max_tokens`` fits.

    * ``None`` in -> ``None`` out (uncapped requests remain uncapped).
    * Unknown/non-positive windows or non-positive caps are returned unchanged.
    * Otherwise caps *configured_max_tokens* to ``response_reserve(...)`` so
      strict servers (vLLM, Ollama, LM Studio) never reject requests with 400
      when ``max_tokens`` exceeds the reserved output headroom.
    """
    if configured_max_tokens is None or configured_max_tokens <= 0:
        return configured_max_tokens
    if max_context_tokens <= 0:
        return configured_max_tokens
    reserve = response_reserve(max_context_tokens, configured_max_tokens)
    if reserve <= 0:
        return configured_max_tokens
    return min(int(configured_max_tokens), reserve)
