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

Hardware target: 16GB VRAM.
  - gemma-4-31b (q4): ~15.5GB  — FRONTIER (fits on single 16GB GPU)
  - gemma-4-26b-a4b (q4): ~13GB — FRONTIER (MoE, 3.8B active, fast)
  - gemma-4-e4b (fp16): ~8GB   — SMALL (128K ctx, agentic-capable)
  - gemma-4-e2b (fp16): ~4GB   — SMALL (128K ctx, mobile-class)
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional


class ModelTier(str, Enum):
    """Model capability tier — drives tool list size and prompt format."""

    NANO = "nano"  # ≤7B params / ≤4K context
    SMALL = "small"  # 7–14B / 4–16K context
    MEDIUM = "medium"  # 14–70B / 16–128K context
    LARGE = "large"  # >70B / >128K context
    FRONTIER = "frontier"  # Cloud frontier (GPT-4o, Claude Opus, Gemini Ultra)


# Maximum number of tools to include in context for each tier.
_TOOL_LIMITS: dict[ModelTier, int] = {
    ModelTier.NANO: 8,
    ModelTier.SMALL: 20,
    ModelTier.MEDIUM: 35,
    ModelTier.LARGE: 50,
    ModelTier.FRONTIER: 60,
}

# Patterns that indicate a FRONTIER cloud model.
# These are matched before the NANO/SMALL keyword heuristics so that e.g.
# "gpt-4o-mini" (cloud, "mini" substring) correctly resolves to FRONTIER
# instead of being demoted to NANO by the keyword fallback.
_FRONTIER_PATTERNS = re.compile(
    r"gpt-4o|gpt-4\.5|gpt-3\.5-turbo|o1|o3|o4"
    r"|claude-opus|claude-3-opus|claude-sonnet|claude-haiku|claude-3"
    r"|gemini-ultra|gemini-2\.[0-9]|gemini-flash|gemini-pro|gemini-exp"
    r"|deepseek-r2"
    # Gemma 4 31B dense — frontier class (fits 16GB in Q4; 80% LiveCodeBench).
    # Note: 26B A4B is MEDIUM (4B active params, 77% LCB — MEDIUM capability at
    # SMALL inference speed; classified separately in _GEMMA4_MEDIUM_PATTERNS).
    r"|gemma-4-31b|gemma4-31b|gemma4:31b",
    re.IGNORECASE,
)

# Patterns that indicate Gemma 4 26B A4B (MoE with 4B active parameters).
# Classifies as MEDIUM: 77% LiveCodeBench, 256K context, but 4B-speed inference,
# no parallel tool calling, fits comfortably on 16GB VRAM alongside other services.
# Covers HuggingFace (gemma-4-26b-a4b-it), LM Studio (gemma-4-26b-a4b-it).
_GEMMA4_MEDIUM_PATTERNS = re.compile(
    r"gemma-4-26b|gemma4-26b|gemma4:26b|gemma-4.*a4b|gemma4.*a4b",
    re.IGNORECASE,
)

# Patterns that indicate a Gemma 4 edge model (E2B / E4B).
# These are small on-device models but with 128K context windows and native
# function-calling support — more capable than a generic NANO 7B model.
# They classify as SMALL (not NANO) so they get full pipeline + 20-tool limit.
# Covers HuggingFace (gemma-4-e4b-it), Ollama (gemma4:e4b), LM Studio (gemma-4-e4b-it).
_GEMMA4_EDGE_PATTERNS = re.compile(
    r"gemma-4-e[24]b|gemma4-e[24]b|gemma4:e[24]b|gemma-4.*e[24]b",
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

    # ── Gemma 4 26B A4B (MoE, 4B active) ────────────────────────────────────
    # Must be checked before param-count extraction: "26b" → 26B → MEDIUM
    # which happens to be correct, but the A4B suffix can also match "a4b" → 4B → NANO
    # (wrong). Explicit pattern check ensures correct MEDIUM classification.
    if _GEMMA4_MEDIUM_PATTERNS.search(name_lower):
        return ModelTier.MEDIUM

    # ── Gemma 4 edge models (E2B / E4B) ─────────────────────────────────────
    # Check before param-count extraction: "e4b" would be parsed as 4B → NANO,
    # but these models have 128K context and native function-calling — SMALL is
    # the correct tier.
    if _GEMMA4_EDGE_PATTERNS.search(name_lower):
        return ModelTier.SMALL

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
    # Never classify below MEDIUM for models with >64K context — large context
    # windows indicate modern, capable models regardless of apparent size.
    if context_window > 0:
        if context_window <= 4096:
            return ModelTier.NANO
        if context_window <= 16384:
            return ModelTier.SMALL
        if context_window <= 131072:
            return ModelTier.MEDIUM
        if context_window <= 200000:
            return ModelTier.LARGE   # 256K models (Gemma 4 26B A4B, Gemma 4 31B)
        return ModelTier.FRONTIER    # >200K = frontier-class context

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


# GAP-FRONTIER-6: Tier-dependent planning step limit.
# Frontier models should not be capped at 8 steps — complex tasks require more.
_STEP_LIMITS: dict[ModelTier, int] = {
    ModelTier.NANO: 4,
    ModelTier.SMALL: 6,
    ModelTier.MEDIUM: 10,
    ModelTier.LARGE: 16,
    ModelTier.FRONTIER: 20,
}


def get_plan_step_limit(tier: ModelTier) -> int:
    """Return the maximum number of plan steps allowed for *tier*.

    GAP-FRONTIER-6: Frontier models (20 steps) vs small models (4–6 steps).
    Used by planning_node to inject the correct limit into the strategic prompt.
    """
    return _STEP_LIMITS.get(tier, 10)


# GAP-9: Tier-dependent max_turns default.
# SMALL models (e.g. Gemma 4 E4B) exhaust context in ~25 turns.
# FRONTIER models (Gemma 4 31B/26B, Claude, GPT-4o) support longer runs.
# These are defaults only — project maxTurns and --max-turns CLI always win.
_MAX_TURNS: dict[ModelTier, int] = {
    ModelTier.NANO: 15,  # 7K–16K context; overflows quickly
    ModelTier.SMALL: 25,  # 32K–128K context; moderate task length
    ModelTier.MEDIUM: 40,  # 128K context; longer tasks feasible
    ModelTier.LARGE: 60,  # >128K context; complex multi-file tasks
    ModelTier.FRONTIER: 80,  # 256K context; long autonomous runs
}


def get_max_turns(tier: ModelTier) -> int:
    """Return the default max_turns for *tier* (GAP-9).

    This is the tier-aware fallback when no project-level or CLI override is set.
    NANO/SMALL models use lower limits to avoid context overflow and wasted compute.
    FRONTIER models use higher limits to allow long autonomous runs on large context windows.
    """
    return _MAX_TURNS.get(tier, 50)
