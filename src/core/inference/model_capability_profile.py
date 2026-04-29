"""model_capability_profile.py — Rich model profiles with VRAM/KV data.

Extends model_tiers.py with:
- Weight size calculations (Q4/Q6/Q8)
- KV cache estimates
- Architecture-aware settings (MoE vs dense)

Primary targets:
- gemma-4-27b-a4b (MoE, 4B active, ~13GB Q4) — MEDIUM tier
- qwen3.5-9b (GDN, ~5.5GB Q4) — SMALL tier
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .model_tiers import ModelTier, classify_model


class Architecture(str, Enum):
    DENSE = "dense"  # Standard dense transformer (Qwen, Llama)
    MOE = "moe"  # Mixture of Experts (Gemma 4 A4B)
    GDN = "gdn"  # Gradient Deferred Notification (Qwen3.5)


class ThinkingMode(str, Enum):
    OFF = "off"  # No thinking (small local models)
    AUTO = "auto"  # Enabled for multi-step tasks
    ON = "on"  # Always on (debug, complex tasks)


@dataclass
class ModelProfile:
    name: str
    architecture: Architecture
    params_total: float  # Billions of parameters
    params_active: Optional[float] = None  # For MoE: active params only
    quantization: str = "q4"  # q4, q6, q8, fp16

    reasoning_score: float = 0.7  # 0-1, reasoning capability
    tool_call_reliability: float = 0.8  # 0-1, tool calling accuracy

    max_context: int = 32768  # Context window in tokens
    kv_per_token_mb: float = 1.6  # KV cache per 1K tokens

    tool_limit: int = 20
    max_turns: int = 30
    thinking_mode: ThinkingMode = ThinkingMode.AUTO
    native_tool_format: str = "json"  # json, xml, yaml

    weights_gb: Optional[float] = None  # Computed from params + quantization
    safe_context_tokens: Optional[int] = None  # VRAM-constrained context

    @property
    def params_display(self) -> str:
        if self.params_active and self.params_active != self.params_total:
            return f"{self.params_total}B({self.params_active}B active)"
        return f"{self.params_total}B"

    def compute_weights_gb(self) -> float:
        if self.weights_gb is not None:
            return self.weights_gb
        bits_per_param = {"q4": 4, "q6": 6, "q8": 8, "fp16": 16}.get(
            self.quantization, 4
        )
        bytes_per_param = bits_per_param / 8
        return self.params_total * bytes_per_param

    def compute_kv_cache_gb(self, tokens: int) -> float:
        return (tokens / 1000) * self.kv_per_token_mb / 1024

    def estimate_safe_context(self, vram_gb: float, overhead_gb: float = 1.5) -> int:
        available = vram_gb - self.compute_weights_gb() - overhead_gb
        if available <= 0:
            return 8192
        tokens_per_gb = 1024 / self.kv_per_token_mb
        safe_tokens = int(available * tokens_per_gb)
        return min(safe_tokens, self.max_context)


# Primary target profiles — these override generic model_tiers lookup
_PRIMARY_PROFILES: dict[str, ModelProfile] = {
    "gemma-4-27b-a4b": ModelProfile(
        name="gemma-4-27b-a4b",
        architecture=Architecture.MOE,
        params_total=27.0,
        params_active=4.0,
        quantization="q4",
        reasoning_score=0.82,
        tool_call_reliability=0.85,
        max_context=131072,
        kv_per_token_mb=1.6,
        tool_limit=35,
        max_turns=50,
        thinking_mode=ThinkingMode.AUTO,
        native_tool_format="gemma4",
        weights_gb=2.0,  # MoE: only 4B active params loaded × 0.5 bytes (Q4)
    ),
    "qwen3.5-9b": ModelProfile(
        name="qwen3.5-9b",
        architecture=Architecture.GDN,
        params_total=9.0,
        params_active=None,
        quantization="q4",
        reasoning_score=0.78,
        tool_call_reliability=0.85,
        max_context=262144,  # 262K native, extensible to 1M
        kv_per_token_mb=0.05,  # GDN linear attention = constant KV (not growing with context!)
        tool_limit=50,  # LARGE tier
        max_turns=60,
        thinking_mode=ThinkingMode.OFF,
        native_tool_format="qwen3",
        weights_gb=6.6,
    ),
    "qwen3-14b": ModelProfile(
        name="qwen3-14b",
        architecture=Architecture.DENSE,
        params_total=14.0,
        params_active=None,
        quantization="q4",
        reasoning_score=0.78,
        tool_call_reliability=0.80,
        max_context=65536,
        kv_per_token_mb=1.4,
        tool_limit=25,
        max_turns=35,
        thinking_mode=ThinkingMode.AUTO,
        native_tool_format="qwen3",
        weights_gb=8.2,
    ),
    "gemma-4-4b": ModelProfile(
        name="gemma-4-4b",
        architecture=Architecture.DENSE,
        params_total=4.0,
        params_active=None,
        quantization="q4",
        reasoning_score=0.65,
        tool_call_reliability=0.70,
        max_context=32768,
        kv_per_token_mb=1.0,
        tool_limit=12,
        max_turns=20,
        thinking_mode=ThinkingMode.OFF,
        native_tool_format="json",
        weights_gb=2.5,
    ),
    "gpt-4o": ModelProfile(
        name="gpt-4o",
        architecture=Architecture.DENSE,
        params_total=200.0,  # Unknown actual, estimate
        params_active=None,
        quantization="fp16",  # Cloud uses fp16/bfloat16
        reasoning_score=0.95,
        tool_call_reliability=0.95,
        max_context=128000,
        kv_per_token_mb=2.0,
        tool_limit=60,
        max_turns=80,
        thinking_mode=ThinkingMode.AUTO,
        native_tool_format="json",
        weights_gb=0,  # Cloud: no local weights
    ),
    "claude": ModelProfile(
        name="claude",
        architecture=Architecture.DENSE,
        params_total=200.0,
        params_active=None,
        quantization="fp16",
        reasoning_score=0.95,
        tool_call_reliability=0.95,
        max_context=200000,
        kv_per_token_mb=2.0,
        tool_limit=60,
        max_turns=80,
        thinking_mode=ThinkingMode.AUTO,
        native_tool_format="json",
        weights_gb=0,
    ),
}


def get_model_profile(model_name: str, context_window: int = 0) -> ModelProfile:
    """Get ModelProfile for a model name.

    First checks primary profiles (exact match), then falls back to
    model_tiers.py classification with derived defaults.
    """
    name_lower = model_name.lower()

    # 1. Exact primary profile match
    for key in _PRIMARY_PROFILES:
        if key in name_lower:
            return _PRIMARY_PROFILES[key]

    # 2. Fallback: derive from model_tiers
    tier = classify_model(model_name, context_window)

    # Derive profile from tier
    if tier == ModelTier.SMALL:
        params = 14
        kv = 1.2
        weights = 8.0
    elif tier == ModelTier.MEDIUM:
        params = 14
        kv = 1.2
        weights = 8.0
    elif tier == ModelTier.MEDIUM:
        params = 27
        kv = 1.6
        weights = 14.0
    elif tier == ModelTier.LARGE:
        params = 70
        kv = 2.0
        weights = 35.0
    else:  # FRONTIER
        params = 200
        kv = 2.0
        weights = 0  # Cloud

    return ModelProfile(
        name=model_name,
        architecture=Architecture.DENSE,
        params_total=params,
        quantization="q4" if weights > 0 else "fp16",
        max_context=context_window or (131072 if tier == ModelTier.MEDIUM else 32768),
        kv_per_token_mb=kv,
        tool_limit={  # type: ignore
            ModelTier.SMALL: 20,
            ModelTier.MEDIUM: 35,
            ModelTier.LARGE: 50,
            ModelTier.FRONTIER: 60,
        }[tier],
        max_turns={  # type: ignore
            ModelTier.SMALL: 25,
            ModelTier.MEDIUM: 40,
            ModelTier.LARGE: 60,
            ModelTier.FRONTIER: 80,
        }[tier],
        thinking_mode=ThinkingMode.AUTO,
        weights_gb=weights,
    )


def list_available_profiles() -> list[str]:
    """List all primary profile names."""
    return list(_PRIMARY_PROFILES.keys())


@dataclass
class AgentModeSettings:
    max_turns: int
    tool_limit: int
    context_tokens: int
    thinking_mode: ThinkingMode
    verification: bool
    replan: bool
    vector_memory: bool


class AgentMode(Enum):
    LITE = "lite"  # ≤14B models
    STANDARD = "standard"  # 14-70B models
    FULL = "full"  # Cloud frontier


_MODE_SETTINGS: dict[AgentMode, AgentModeSettings] = {
    AgentMode.LITE: AgentModeSettings(
        max_turns=20,
        tool_limit=15,
        context_tokens=16384,
        thinking_mode=ThinkingMode.OFF,
        verification=False,
        replan=False,
        vector_memory=False,
    ),
    AgentMode.STANDARD: AgentModeSettings(
        max_turns=40,
        tool_limit=35,
        context_tokens=32768,
        thinking_mode=ThinkingMode.AUTO,
        verification=True,
        replan=True,
        vector_memory=True,
    ),
    AgentMode.FULL: AgentModeSettings(
        max_turns=80,
        tool_limit=60,
        context_tokens=128000,
        thinking_mode=ThinkingMode.ON,
        verification=True,
        replan=True,
        vector_memory=True,
    ),
}


def select_agent_mode(params_b: float, is_local: bool = True) -> AgentMode:
    """Select AgentMode based on parameter count and deployment type."""
    if not is_local:
        return AgentMode.FULL
    if params_b <= 14:
        return AgentMode.LITE
    return AgentMode.STANDARD


def get_agent_mode_settings(mode: AgentMode) -> AgentModeSettings:
    """Get default settings for an AgentMode."""
    return _MODE_SETTINGS[mode]
