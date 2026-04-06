"""model_tiers.py — Model capability tier classification.

S1-A: Provides ``ModelTier`` and ``classify_model()`` so that nodes and
``ContextBuilder`` can adapt tool lists, prompt formats, and token budgets
based on the size of the active model.

Tiers:
- NANO     ≤7B / ≤4K context — YAML tools, simple_mode, 8-tool limit
- SMALL    7–14B / 4–16K     — YAML tools, full pipeline, 20-tool limit
- MEDIUM   14–70B / 16–128K  — JSON tools, full pipeline, 35-tool limit
- LARGE    >70B / >128K      — JSON tools, parallel calls, 50-tool limit
- FRONTIER Cloud frontier     — JSON tools, parallel calls, 60-tool limit
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional


class ModelTier(str, Enum):
    """Model capability tier — drives tool list size and prompt format."""

    NANO = "nano"         # ≤7B params / ≤4K context
    SMALL = "small"       # 7–14B / 4–16K context
    MEDIUM = "medium"     # 14–70B / 16–128K context
    LARGE = "large"       # >70B / >128K context
    FRONTIER = "frontier" # Cloud frontier (GPT-4o, Claude Opus, Gemini Ultra)


# Maximum number of tools to include in context for each tier.
_TOOL_LIMITS: dict[ModelTier, int] = {
    ModelTier.NANO: 8,
    ModelTier.SMALL: 20,
    ModelTier.MEDIUM: 35,
    ModelTier.LARGE: 50,
    ModelTier.FRONTIER: 60,
}

# Patterns that indicate a FRONTIER cloud model.
_FRONTIER_PATTERNS = re.compile(
    r"gpt-4o|o1|o3|o4|claude-opus|claude-3-opus|claude-sonnet|gemini-ultra"
    r"|gemini-2\.0-pro|gemini-exp|deepseek-r2",
    re.IGNORECASE,
)

# Patterns that include the parameter count in the model name (e.g. "7b", "70b").
_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)b", re.IGNORECASE)


def _extract_param_count(model_name: str) -> Optional[float]:
    """Extract the parameter count (in billions) from the model name, or None."""
    m = _PARAM_RE.search(model_name)
    if m:
        return float(m.group(1))
    return None


def classify_model(model_name: str, context_window: int = 0) -> ModelTier:
    """Classify a model into a tier based on name heuristics and context window.

    Args:
        model_name:     The model identifier string (e.g. ``"qwen3:14b"``,
                        ``"gpt-4o"``, ``"llama-3.1-70b"``).
        context_window: Known context window in tokens.  If 0 (unknown), the
                        tier is inferred from the name alone.

    Returns:
        The best-guess ``ModelTier``.
    """
    name_lower = model_name.lower()

    # ── Frontier cloud models ────────────────────────────────────────────────
    if _FRONTIER_PATTERNS.search(name_lower):
        return ModelTier.FRONTIER

    # ── Parameter-count based classification ─────────────────────────────────
    params = _extract_param_count(name_lower)
    if params is not None:
        if params <= 7:
            return ModelTier.NANO
        if params <= 14:
            return ModelTier.SMALL
        if params <= 70:
            return ModelTier.MEDIUM
        return ModelTier.LARGE

    # ── Context-window fallback ───────────────────────────────────────────────
    if context_window > 0:
        if context_window <= 4096:
            return ModelTier.NANO
        if context_window <= 16384:
            return ModelTier.SMALL
        if context_window <= 131072:
            return ModelTier.MEDIUM
        return ModelTier.LARGE

    # ── Name keyword heuristics ───────────────────────────────────────────────
    if any(k in name_lower for k in ("mini", "tiny", "nano", "phi-2", "phi2")):
        return ModelTier.NANO
    if any(k in name_lower for k in ("small", "lite", "1b", "2b", "3b")):
        return ModelTier.SMALL
    if any(k in name_lower for k in ("large", "medium", "base", "mistral-7b")):
        return ModelTier.MEDIUM

    # Default: MEDIUM (safe middle ground for unknown local models)
    return ModelTier.MEDIUM


def get_tool_limit(tier: ModelTier) -> int:
    """Return the maximum number of tools to include in context for *tier*."""
    return _TOOL_LIMITS[tier]


def supports_native_tools(tier: ModelTier) -> bool:
    """Return True if the tier is expected to support JSON native tool calling."""
    return tier in (ModelTier.MEDIUM, ModelTier.LARGE, ModelTier.FRONTIER)


def is_simple_mode(tier: ModelTier) -> bool:
    """Return True if the tier should use simple_mode (YAML, single tool/message)."""
    return tier == ModelTier.NANO
