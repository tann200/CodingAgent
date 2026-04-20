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
from pathlib import Path

logger = logging.getLogger(__name__)

# TASK-18: Model pricing table — (input $/1k tokens, output $/1k tokens).
# Prices sourced from public provider documentation; update as needed.
_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (0.0025, 0.010),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.010, 0.030),
    "gpt-4": (0.030, 0.060),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "o1": (0.015, 0.060),
    "o1-mini": (0.003, 0.012),
    "o3-mini": (0.001, 0.004),
    # Anthropic
    "claude-3-5-sonnet-20241022": (0.003, 0.015),
    "claude-3-5-haiku-20241022": (0.0008, 0.004),
    "claude-3-opus-20240229": (0.015, 0.075),
    "claude-3-sonnet-20240229": (0.003, 0.015),
    "claude-3-haiku-20240307": (0.00025, 0.00125),
    # Gemini (Google)
    "gemini-1.5-pro": (0.00125, 0.005),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "gemini-2.0-flash": (0.0001, 0.0004),
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


def set_active_context_length(length: int) -> None:
    """Override the active context length with a live value from the models endpoint.

    Called by the TUI bridge (and any other subscriber of the
    ``provider.context_window`` event) as soon as the real loaded context length
    is known.  A value of 0 or negative clears the override so the file-based
    fallback is used again.
    """
    global _live_context_length
    _live_context_length = max(0, int(length))
    logger.debug(f"provider_context: live context length set to {_live_context_length}")


def _load_active_context_length() -> int:
    """Return the context_length of the active provider, or a sensible default.

    Prefers the live value set by ``set_active_context_length()`` (populated
    from the models endpoint at runtime) over the static providers.json entry.
    """
    # LIVE-CTX: prefer runtime value from models endpoint
    if _live_context_length > 0:
        return _live_context_length

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
# NANO models need to reserve more for their short output (4K ctx → output needs ~50%).
# Frontier models with 200K+ context can allocate 80% to the prompt safely.
_TIER_CONTEXT_FRACTION: dict[str, float] = {
    "nano": 0.50,  # ≤7B / ≤8K ctx — output needs half the window
    "small": 0.60,  # 7–14B / 8–32K ctx
    "medium": 0.70,  # 14–70B / 32–128K ctx
    "large": 0.75,  # 70B+ / 128K+ ctx
    "frontier": 0.80,  # cloud / 31B+ / 256K ctx
}


def get_context_budget(
    fraction: float = 0.65,
    min_tokens: int = 6000,
    max_tokens: int = 131072,
    model_tier: str = "",
) -> int:
    """
    Return a token budget for context-building based on the active provider's
    context window.

    Prefers the live context length reported by the models endpoint (set via
    ``set_active_context_length()``) over the static providers.json value.

    P3-F: When *model_tier* is provided, overrides *fraction* with the
    tier-specific value from ``_TIER_CONTEXT_FRACTION`` so that NANO models
    (which have tiny context windows) get a smaller budget than frontier models
    that have 200K+ tokens to work with.

    fraction   — default fraction when model_tier is absent or unknown (0.65)
    min_tokens — lower clamp (guarantees a usable minimum even for tiny models)
    max_tokens — upper clamp (avoids bloated prompts for very large context windows)
    model_tier — optional tier string (e.g. "nano", "small", "frontier")
    """
    if model_tier:
        fraction = _TIER_CONTEXT_FRACTION.get(model_tier.lower(), fraction)
    ctx_len = _load_active_context_length()
    budget = int(ctx_len * fraction)
    return max(min_tokens, min(budget, max_tokens))
