"""kv_cache_governor.py — VRAM-aware KV cache management.

Monitors KV cache usage and triggers compaction before OOM.
For small local models (especially Qwen), KV cache growth is the real bottleneck.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class CompactionAction(str, Enum):
    NONE = "none"
    WARNING = "warning"  # Token usage > 70%
    COMPACT = "compact"  # Token usage > 85%
    FORCE_COMPACT = "force_compact"  # KV cache approaching limit


@dataclass
class KVCacheState:
    """Current KV cache state."""

    current_tokens: int
    max_tokens: int
    kv_cache_gb: float
    max_kv_cache_gb: float

    @property
    def usage_ratio(self) -> float:
        if self.max_tokens <= 0:
            return 0.0
        return self.current_tokens / self.max_tokens

    @property
    def vram_usage_ratio(self) -> float:
        if self.max_kv_cache_gb <= 0:
            return 0.0
        return self.kv_cache_gb / self.max_kv_cache_gb

    @property
    def should_warn(self) -> bool:
        return self.usage_ratio >= 0.70 or self.vram_usage_ratio >= 0.70

    @property
    def should_compact(self) -> bool:
        return self.usage_ratio >= 0.85 or self.vram_usage_ratio >= 0.85

    @property
    def action(self) -> CompactionAction:
        if self.vram_usage_ratio >= 0.95:
            return CompactionAction.FORCE_COMPACT
        if self.should_compact:
            return CompactionAction.COMPACT
        if self.should_warn:
            return CompactionAction.WARNING
        return CompactionAction.NONE


class KVCacheGovernor:
    """Monitors KV cache and triggers compaction based on VRAM limits.

    For small local models, the KV cache grows rapidly with each turn.
    This governor ensures we compact before hitting VRAM limits.

    Usage:
        governor = KVCacheGovernor(
            max_vram_gb=16.0,
            model_weights_gb=5.5,
            kv_per_token_mb=1.2,
        )

        # After each turn
        state = governor.on_context_update(current_tokens=5000)
        if state.action != CompactionAction.NONE:
            compact(messages)
    """

    def __init__(
        self,
        max_vram_gb: float,
        model_weights_gb: float,
        kv_per_token_mb: float = 1.2,
        overhead_gb: float = 0.5,
        warning_threshold: float = 0.70,
        compact_threshold: float = 0.85,
    ):
        self.max_vram_gb = max_vram_gb
        self.model_weights_gb = model_weights_gb
        self.kv_per_token_mb = kv_per_token_mb
        self.overhead_gb = overhead_gb

        self.max_kv_cache_gb = max_vram_gb - model_weights_gb - overhead_gb
        self.max_tokens = int((self.max_kv_cache_gb * 1024 / kv_per_token_mb) * 1000)

        self.warning_threshold = warning_threshold
        self.compact_threshold = compact_threshold

        self._current_tokens = 0
        self._last_action = CompactionAction.NONE
        self._compaction_count = 0

    @property
    def current_state(self) -> KVCacheState:
        kv_gb = (self._current_tokens / 1000) * self.kv_per_token_mb / 1024
        return KVCacheState(
            current_tokens=self._current_tokens,
            max_tokens=self.max_tokens,
            kv_cache_gb=kv_gb,
            max_kv_cache_gb=self.max_kv_cache_gb,
        )

    def on_context_update(self, current_tokens: int) -> KVCacheState:
        """Called after each context update to check KV cache state."""
        self._current_tokens = current_tokens
        state = self.current_state

        if state.action != CompactionAction.NONE:
            logger.info(
                f"KVCacheGovernor: action={state.action.value}, "
                f"tokens={current_tokens}/{self.max_tokens} "
                f"({state.usage_ratio:.1%}), "
                f"vram={state.kv_cache_gb:.2f}/{self.max_kv_cache_gb:.2f}GB "
                f"({state.vram_usage_ratio:.1%})"
            )

        self._last_action = state.action
        return state

    def estimate_tokens_for_vram(self, vram_gb: float) -> int:
        """Estimate max tokens that fit in available VRAM."""
        available = vram_gb - self.model_weights_gb - self.overhead_gb
        if available <= 0:
            return 8192
        return int((available * 1024 / self.kv_per_token_mb) * 1000)

    def get_status(self) -> dict:
        """Get human-readable status dict."""
        state = self.current_state
        return {
            "current_tokens": self._current_tokens,
            "max_tokens": self.max_tokens,
            "usage_ratio": f"{state.usage_ratio:.1%}",
            "vram_usage_gb": f"{state.kv_cache_gb:.2f}",
            "max_vram_gb": f"{self.max_kv_cache_gb:.2f}",
            "action": state.action.value,
            "compaction_count": self._compaction_count,
        }

    def reset(self) -> None:
        """Reset state after a compaction has been applied."""
        self._compaction_count += 1
        self._current_tokens = 0
        self._last_action = CompactionAction.NONE

    def project_tokens(self, incoming_tokens: int) -> int:
        """Project token count after adding incoming tokens."""
        return self._current_tokens + incoming_tokens

    def will_overflow(self, incoming_tokens: int) -> bool:
        """Check if adding incoming_tokens would exceed limits."""
        projected = self.project_tokens(incoming_tokens)
        projected_vram = (projected / 1000) * self.kv_per_token_mb / 1024
        return projected_vram > self.max_kv_cache_gb


def create_governor_for_model(
    model_name: str,
    hardware_name: str = "auto",
) -> Optional[KVCacheGovernor]:
    """Create a KVCacheGovernor based on model and hardware profiles.

    Returns None for cloud models (no local VRAM constraints).
    """
    try:
        from src.core.inference import get_runtime_profile

        runtime = get_runtime_profile(model_name, hardware_name)
        if runtime.is_cloud:
            return None  # No local VRAM constraints

        model = runtime.model
        hardware = runtime.hardware

        # Skip if VRAM is 0 or model weights are 0 (cloud fallback)
        if hardware.vram_gb <= 0 or model.compute_weights_gb() <= 0:
            return None

        return KVCacheGovernor(
            max_vram_gb=hardware.vram_gb,
            model_weights_gb=model.compute_weights_gb(),
            kv_per_token_mb=model.kv_per_token_mb,
        )
    except Exception as e:
        logger.debug(f"KVCacheGovernor: failed to create for {model_name}: {e}")
        return None
