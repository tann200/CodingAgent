"""runtime_profile.py — Merge model + hardware into runtime decisions.

RuntimeProfile = model_profile × hardware_profile → runtime settings

This drives:
- context size (safe_context_tokens)
- tool count (tool_limit)
- loop depth (max_turns)
- thinking mode (thinking_mode)
- compaction strategy (compaction_threshold)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .hardware_capability_profile import (
    HardwareProfile,
    get_hardware_profile,
    compute_safe_context_tokens,
)
from .model_tiers import ModelTier, get_tier_config, get_tool_limit, get_max_turns
from .model_capability_profile import (
    ModelProfile,
    get_model_profile,
    ThinkingMode,
)

logger = logging.getLogger(__name__)


@dataclass
class RuntimeProfile:
    """Merged profile driving runtime decisions."""

    model: ModelProfile
    hardware: HardwareProfile

    model_tier: ModelTier

    safe_context_tokens: int
    tool_limit: int
    max_turns: int
    thinking_mode: ThinkingMode

    use_verification: bool
    use_replan: bool
    use_vector_memory: bool

    kv_cache_quant: bool = False
    compaction_threshold: float = 0.8

    @property
    def is_cloud(self) -> bool:
        return self.hardware.vram_gb == 0

    @property
    def is_small_model(self) -> bool:
        return self.model.params_total <= 14

    def compute_vram_usage(self, context_tokens: int) -> float:
        weights = self.model.compute_weights_gb()
        kv = self.model.compute_kv_cache_gb(context_tokens)
        overhead = 1.5
        return weights + kv + overhead

    def will_fit_in_vram(self, context_tokens: int) -> bool:
        usage = self.compute_vram_usage(context_tokens)
        return usage <= self.hardware.vram_gb


def merge_profiles(
    model_profile: ModelProfile,
    hardware_profile: HardwareProfile,
) -> RuntimeProfile:
    """Merge model + hardware into runtime profile.

    Tier settings are sourced from ``ModelTier`` (``model_tiers``), not
    the deprecated ``AgentMode``.
    """
    from .model_tiers import classify_model

    model_tier = classify_model(model_profile.name, model_profile.max_context)
    tc = get_tier_config(model_tier)

    safe_context = compute_safe_context_tokens(
        vram_gb=hardware_profile.vram_gb,
        model_weights_gb=model_profile.compute_weights_gb(),
        kv_per_token_mb=model_profile.kv_per_token_mb,
        overhead_gb=0.5,
    )

    safe_context = min(safe_context, tc.context_tokens)
    safe_context = min(safe_context, model_profile.max_context)

    tool_limit = min(get_tool_limit(model_tier), model_profile.tool_limit)
    max_turns = min(get_max_turns(model_tier), model_profile.max_turns)

    thinking_mode = ThinkingMode(tc.thinking_mode)
    if model_profile.thinking_mode == ThinkingMode.OFF:
        thinking_mode = ThinkingMode.OFF

    return RuntimeProfile(
        model=model_profile,
        hardware=hardware_profile,
        model_tier=model_tier,
        safe_context_tokens=safe_context,
        tool_limit=tool_limit,
        max_turns=max_turns,
        thinking_mode=thinking_mode,
        use_verification=tc.verification,
        use_replan=tc.replan,
        use_vector_memory=tc.vector_memory,
        kv_cache_quant=hardware_profile.vram_gb <= 16,
        compaction_threshold=0.8 if model_tier == ModelTier.SMALL else 0.85,
    )


def get_runtime_profile(
    model_name: str,
    hardware_name: str = "auto",
    context_window: int = 0,
) -> RuntimeProfile:
    """Get runtime profile from model name + hardware.

    Args:
        model_name: Model identifier (e.g., "gemma-4-27b-a4b", "qwen3.5-9b")
        hardware_name: Hardware profile name or "auto" for detection
        context_window: Known context window override
    """
    model = get_model_profile(model_name, context_window)
    hardware = get_hardware_profile(hardware_name)

    return merge_profiles(model, hardware)


def preview_runtime(model_name: str, vram_gb: float, quant: str = "q4") -> dict:
    """Preview runtime settings for a model + VRAM combination.

    Useful for CLI --dry-run or testing different configurations.
    """
    from .model_capability_profile import Architecture

    try:
        # Extract param count from model name (e.g. "qwen3.5-9b" → 9.0)
        _params = float(model_name.split("-")[1].replace("b", "").replace("B", ""))
    except (IndexError, ValueError, AttributeError):
        _params = 7.0  # fallback: assume small model
    model = ModelProfile(
        name=model_name,
        architecture=Architecture.DENSE,
        params_total=_params,
        quantization=quant,
        weights_gb=None,
    )
    hardware = HardwareProfile(
        name=f"preview-{vram_gb}g",
        vram_gb=vram_gb,
        ram_gb=0,
        cpu_cores=0,
    )

    runtime = merge_profiles(model, hardware)

    return {
        "model": model_name,
        "vram_gb": vram_gb,
        "model_tier": runtime.model_tier.value,
        "safe_context_tokens": runtime.safe_context_tokens,
        "tool_limit": runtime.tool_limit,
        "max_turns": runtime.max_turns,
        "thinking_mode": runtime.thinking_mode.value,
        "will_fit": runtime.will_fit_in_vram(runtime.safe_context_tokens),
    }
