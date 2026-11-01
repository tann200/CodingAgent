"""model_tiers.py — Model capability tier classification.

S1-A: Provides ``ModelTier`` and ``classify_model()`` so that nodes and
``ContextBuilder`` can adapt tool lists, prompt formats, and token budgets
based on the size of the active model.

Tiers:
- SMALL    7–14B / 16K     — JSON tools, full pipeline, 20-tool limit
- MEDIUM   14–70B / 32K  — JSON tools, full pipeline, 35-tool limit
- LARGE    >70B / 64K      — JSON tools, parallel calls, 50-tool limit
- FRONTIER Cloud frontier     — JSON tools, parallel calls, 60-tool limit

Hardware target: 16GB VRAM.
  - qwen3:14b (q4): ~8GB — MEDIUM (16K-32K ctx)
  - qwen3:9b (q4): ~6GB — SMALL (16K ctx)
  - gemma-4-31b (q4): ~15.5GB  — FRONTIER (fits on single 16GB GPU)
  - gemma-4-26b-a4b (q4): ~13GB — FRONTIER (MoE, 3.8B active, fast)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ModelTier(str, Enum):
    """Model capability tier — drives tool list size and prompt format."""

    SMALL = "small"  # 7–14B / 16K context
    MEDIUM = "medium"  # 14–70B / 32K context
    LARGE = "large"  # >70B / 64K context
    FRONTIER = "frontier"  # Cloud frontier (GPT-4o, Claude Opus, Gemini Ultra)


# Maximum number of tools to include in context for each tier.
# SMALL (7–14B) → 15: conservative limit to avoid overwhelming
#   small models with too many choices (aligned with AgentMode.LITE).
# MEDIUM (14–70B) → 35: spacious enough for the 26 tool-set YAMLs.
# LARGE (>70B) → 50: near-full tool access.
# FRONTIER (cloud) → 60: full tool access.
_TOOL_LIMITS: dict[ModelTier, int] = {
    ModelTier.SMALL: 15,
    ModelTier.MEDIUM: 35,
    ModelTier.LARGE: 50,
    ModelTier.FRONTIER: 60,
}


@dataclass
class TierConfig:
    """Behavioral settings per model tier — replaces ``AgentModeSettings``.

    These drive context budget, thinking mode, and pipeline-stage toggles
    (verification, replan, vector memory) that differ by tier.
    """
    context_tokens: int
    thinking_mode: str  # "off" | "auto" | "on"
    verification: bool
    replan: bool
    vector_memory: bool


_TIER_CONFIGS: dict[ModelTier, TierConfig] = {
    ModelTier.SMALL: TierConfig(16384, "off", False, False, False),
    ModelTier.MEDIUM: TierConfig(32768, "auto", True, True, True),
    ModelTier.LARGE: TierConfig(65536, "auto", True, True, True),
    ModelTier.FRONTIER: TierConfig(128000, "on", True, True, True),
}


def get_tier_config(tier: ModelTier) -> TierConfig:
    """Return the ``TierConfig`` for *tier*."""
    return _TIER_CONFIGS[tier]

# Patterns that indicate legacy / lightweight cloud models that should be
# treated as MEDIUM (standard) rather than full FRONTIER.
_MEDIUM_CLOUD_PATTERNS = re.compile(
    r"gpt-3\.5-turbo|claude-(?:3-haiku|3-5-haiku|3\.5-haiku)",
    re.IGNORECASE,
)

# Patterns that indicate a FRONTIER model (2026 frontier cloud + Gemma 4 31B).
# Checked only when the model is not a local quantized/distilled checkpoint
# carrying an explicit <=70B parameter tag.
_FRONTIER_PATTERNS = re.compile(
    r"gpt-[56](?:\.[0-9]+)?(?:-[a-z0-9]+)?|gpt-4\.5|gpt-4\.1|gpt-4o|\bo[134]\b|\bo[134]-(?:mini|pro|high)\b"
    r"|claude-(?:opus|sonnet|haiku)-[4-6](?:\.[0-9]+)?|claude-[4-6](?:\.[0-9]+)?|claude-opus|claude-3-opus|claude-sonnet|claude-3"
    r"|gemini-[34](?:\.[0-9]+)?|gemini-2\.[5-9]|gemini-ultra|gemini-flash|gemini-pro|gemini-exp"
    r"|deepseek-(?:r[1-3]|v[3-5]|coder-v[3-5]|reasoner)"
    r"|llama-?4[-:]maverick"
    # Gemma 4 31B dense — frontier class (fits 16GB in Q4; 80% LiveCodeBench).
    r"|gemma-4-31b|gemma4-31b|gemma4:31b",
    re.IGNORECASE,
)

_LOCAL_FRONTIER_OVERRIDE_RE = re.compile(
    r"gemma-?4[-:]31b",
    re.IGNORECASE,
)

# Patterns that indicate Llama 4 Scout (MoE, 17B active).
_LLAMA4_SCOUT_PATTERNS = re.compile(
    r"llama-?4[-:]scout",
    re.IGNORECASE,
)

# Patterns that indicate Gemma 4 26B A4B (MoE with 4B active parameters).
_GEMMA4_MEDIUM_PATTERNS = re.compile(
    r"gemma-4-26b|gemma4-26b|gemma4:26b|gemma-4.*a4b|gemma4.*a4b",
    re.IGNORECASE,
)

# Patterns that indicate a Gemma 4 edge model (E2B / E4B).
_GEMMA4_EDGE_PATTERNS = re.compile(
    r"gemma-4-e[24]b|gemma4-e[24]b|gemma4:e[24]b|gemma-4.*e[24]b",
    re.IGNORECASE,
)

# MoE & dense parameter extraction patterns (word-bounded so hex/quant tags do not collide).
_MOE_CLASSIC_RE = re.compile(r"(?<![a-z0-9])(\d+)x(\d+(?:\.\d+)?)b(?![a-z0-9])", re.IGNORECASE)
_MOE_ACTIVE_RE = re.compile(
    r"(?<![a-z0-9])(\d+(?:\.\d+)?)b[-_:]a(\d+(?:\.\d+)?)b(?![a-z0-9])",
    re.IGNORECASE,
)
_EDGE_PARAM_RE = re.compile(r"(?<![a-z0-9])e(\d+(?:\.\d+)?)b(?![a-z0-9])", re.IGNORECASE)
_PARAM_RE = re.compile(r"(?<![a-z0-9])(\d+(?:\.\d+)?)b(?![a-z0-9])", re.IGNORECASE)

_DISTILLED_OR_QUANT_RE = re.compile(
    r"distill|gguf|awq|gptq|exl2",
    re.IGNORECASE,
)


def extract_model_params(model_name: str) -> tuple[Optional[float], Optional[float]]:
    """Extract ``(total_params_b, active_params_b)`` from *model_name*, or ``(None, None)``.

    Handles:
    - Classic MoE ``NxMb`` (e.g. ``mixtral-8x7b``, ``8x22b``)
    - Modern MoE ``Xb-aYb`` (e.g. ``gemma-4-26b-a4b``, ``qwen3.5-35b-a3b``)
    - Edge ``eXb`` tags (e.g. ``gemma-4-e4b``)
    - Dense ``Xb`` tags (e.g. ``qwen3.5-9b``, ``qwen3:14b``, ``llama-3.3-70b``)
    - Well-known un-suffixed local families (``phi-4-mini``, ``phi-4``, ``devstral-small``)
    """
    s = (model_name or "").lower().strip()
    if not s:
        return None, None

    m_moe_active = _MOE_ACTIVE_RE.search(s)
    if m_moe_active:
        total = float(m_moe_active.group(1))
        active = float(m_moe_active.group(2))
        return total, active

    m_moe_classic = _MOE_CLASSIC_RE.search(s)
    if m_moe_classic:
        experts = float(m_moe_classic.group(1))
        per_expert = float(m_moe_classic.group(2))
        total = experts * per_expert
        active = min(total, 2.0 * per_expert)
        return total, active

    m_edge = _EDGE_PARAM_RE.search(s)
    if m_edge:
        val = float(m_edge.group(1))
        return val, val

    m_dense = _PARAM_RE.search(s)
    if m_dense:
        val = float(m_dense.group(1))
        return val, val

    if "phi-4-mini" in s:
        return 3.8, 3.8
    if "phi-4" in s:
        return 14.0, 14.0
    if "devstral-small" in s:
        return 24.0, 24.0
    if "llama-4-scout" in s or "llama4-scout" in s or "llama-4:scout" in s:
        return 109.0, 17.0
    if "llama-4-maverick" in s or "llama4-maverick" in s or "llama-4:maverick" in s:
        return 400.0, 60.0

    return None, None


def _extract_param_count(model_name: str) -> Optional[float]:
    """Extract the total parameter count (in billions) from *model_name*, or None."""
    total, _ = extract_model_params(model_name)
    return total


def is_nano_model(model_name: str) -> bool:
    """Return True for sub-8.5B local models (e.g. ``gemma-4-4b``, ``qwen2.5:7b``).

    Distinguishes sub-8.5B ``nano`` models (which use ``SINGLE_LOOP`` without
    mandatory verification) from 8.5B–14.9B ``compact`` models (e.g.
    ``qwen3.5-9b``, ``gemma-4-12b``, ``qwen3:14b``, ``phi-4``) that support
    12 flat tools and targeted verification.
    """
    s = (model_name or "").lower().strip()
    if not s:
        return False
    total, _ = extract_model_params(s)
    if total is not None:
        return total < 8.5
    return any(k in s for k in ("nano", "tiny"))


def classify_model(model_name: str, context_window: int = 0) -> ModelTier:
    """Classify a model into a tier based on parameter count, family, and context window."""
    name_lower = (model_name or "").lower().strip()
    total_params, _ = extract_model_params(name_lower)
    is_local_distill_or_quant = bool(_DISTILLED_OR_QUANT_RE.search(name_lower))

    # ── Explicit local frontier override (Gemma 4 31B) ───────────────────────
    if _LOCAL_FRONTIER_OVERRIDE_RE.search(name_lower) and not is_local_distill_or_quant:
        return ModelTier.FRONTIER

    # ── Llama 4 Scout (MoE with 17B active parameters) -> MEDIUM (standard) ───
    if _LLAMA4_SCOUT_PATTERNS.search(name_lower):
        return ModelTier.MEDIUM

    # ── Cloud models (only when no local <=70B parameter tag or distill tag) ─
    if not is_local_distill_or_quant and (total_params is None or total_params > 70):
        if _MEDIUM_CLOUD_PATTERNS.search(name_lower):
            return ModelTier.MEDIUM
        if _FRONTIER_PATTERNS.search(name_lower):
            return ModelTier.FRONTIER

    # ── Gemma 4 26B A4B (MoE, 4B active) ─────────────────────────────────────
    if _GEMMA4_MEDIUM_PATTERNS.search(name_lower):
        return ModelTier.MEDIUM

    # ── Gemma 4 edge models (E2B / E4B) ──────────────────────────────────────
    if _GEMMA4_EDGE_PATTERNS.search(name_lower):
        return ModelTier.SMALL

    # ── Parameter-count based classification ─────────────────────────────────
    # Note: Qwen3.5 9B/14B/27B/35B-A3B/72B now classify accurately by parameter
    # count (9B/14B -> SMALL, 27B/35B -> MEDIUM, >70B -> LARGE) rather than
    # forcing 9B models into LARGE (50 tools).
    if total_params is not None:
        if total_params < 15.0:
            return ModelTier.SMALL
        if total_params <= 70.0:
            return ModelTier.MEDIUM
        return ModelTier.LARGE

    # ── Context-window fallback (unknown model name without param tags) ──────
    # Large context windows on local servers (e.g. 262K GDN) must NOT escalate
    # an unknown local model to cloud FRONTIER.
    if context_window > 0:
        if context_window <= 16384:
            return ModelTier.SMALL
        if context_window <= 131072:
            return ModelTier.MEDIUM
        return ModelTier.LARGE

    # ── Name keyword heuristics ──────────────────────────────────────────────
    if any(k in name_lower for k in ("small", "lite", "mini", "nano", "tiny")):
        return ModelTier.SMALL
    if any(k in name_lower for k in ("large", "medium", "base", "mistral", "devstral")):
        return ModelTier.MEDIUM

    return ModelTier.MEDIUM


def get_tool_limit(tier: ModelTier) -> int:
    """Return the maximum number of tools to include in context for *tier*."""
    return _TOOL_LIMITS[tier]


def supports_native_tools(tier: ModelTier) -> bool:
    """Return True if the tier is expected to support JSON native tool calling."""
    return tier in (ModelTier.MEDIUM, ModelTier.LARGE, ModelTier.FRONTIER)


def is_simple_mode(tier: ModelTier) -> bool:
    """Return True if the tier should use simple_mode (single tool/message)."""
    return tier == ModelTier.SMALL


# GAP-FRONTIER-6: Tier-dependent planning step limit.
# Frontier models should not be capped at 8 steps — complex tasks require more.
# Gemma 4 26B A4B: 256K context, ~40 tok/s, MEDIUM tier with LARGE capability.
_STEP_LIMITS: dict[ModelTier, int] = {
    ModelTier.SMALL: 6,
    ModelTier.MEDIUM: 12,  # Increased for 256K context models
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
# Gemma 4 26B A4B has 256K context - more turns allowed.
# These are defaults only — project maxTurns and --max-turns CLI always win.
_MAX_TURNS: dict[ModelTier, int] = {
    ModelTier.SMALL: 25,  # 16K context
    ModelTier.MEDIUM: 50,  # 256K context (Gemma 4 26B A4B)
    ModelTier.LARGE: 60,  # 256K+ context
    ModelTier.FRONTIER: 80,  # 200K context; long autonomous runs
}


def get_max_turns(tier: ModelTier) -> int:
    """Return the default max_turns for *tier* (GAP-9).

    This is the tier-aware fallback when no project-level or CLI override is set.
    NANO/SMALL models use lower limits to avoid context overflow and wasted compute.
    FRONTIER models use higher limits to allow long autonomous runs on large context windows.
    """
    return _MAX_TURNS.get(tier, 50)
