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
    warning_threshold: float = 0.70
    compact_threshold: float = 0.85

    def __post_init__(self) -> None:
        self.warning_threshold = max(0.0, min(1.0, float(self.warning_threshold)))
        self.compact_threshold = max(0.0, min(1.0, float(self.compact_threshold)))
        if self.warning_threshold > self.compact_threshold:
            self.warning_threshold = self.compact_threshold

    @property
    def usage_ratio(self) -> float:
        if self.max_tokens <= 0:
            return 1.0 if self.max_kv_cache_gb <= 0 else 0.0
        return self.current_tokens / self.max_tokens

    @property
    def vram_usage_ratio(self) -> float:
        if self.max_kv_cache_gb <= 0:
            return 1.0 if self.max_tokens <= 0 else 0.0
        return self.kv_cache_gb / self.max_kv_cache_gb

    @property
    def should_warn(self) -> bool:
        return self.usage_ratio >= self.warning_threshold or self.vram_usage_ratio >= self.warning_threshold

    @property
    def should_compact(self) -> bool:
        return self.usage_ratio >= self.compact_threshold or self.vram_usage_ratio >= self.compact_threshold

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
        cpu_token_budget: int = 8192,
        kv_cache_quant: bool | str = False,
        is_moe: bool = False,
    ):
        self.max_vram_gb = max(0.0, float(max_vram_gb))
        self.model_weights_gb = float(model_weights_gb)
        q_scale = 1.0
        if isinstance(kv_cache_quant, str):
            ql = kv_cache_quant.strip().lower()
            if ql in ("q4", "q4_0", "4bit"):
                q_scale = 0.25
            elif ql in ("q8", "q8_0", "8bit", "true", "1"):
                q_scale = 0.5
        elif kv_cache_quant:
            q_scale = 0.5
        moe_scale = 0.35 if is_moe else 1.0
        self.kv_per_token_mb = float(kv_per_token_mb) * q_scale * moe_scale
        self.overhead_gb = float(overhead_gb)

        if self.max_vram_gb == 0.0:
            self.max_kv_cache_gb = 0.0
            self.max_tokens = max(0, int(cpu_token_budget))
        else:
            raw_kv = self.max_vram_gb - self.model_weights_gb - self.overhead_gb
            self.max_kv_cache_gb = max(0.0, raw_kv)
            if self.max_kv_cache_gb <= 0:
                self.max_tokens = 0
            else:
                self.max_tokens = max(0, int((self.max_kv_cache_gb * 1024 / self.kv_per_token_mb) * 1000))

        warn = max(0.0, min(1.0, float(warning_threshold)))
        compact = max(0.0, min(1.0, float(compact_threshold)))
        if warn > compact:
            warn = compact
        self.warning_threshold = warn
        self.compact_threshold = compact

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
            warning_threshold=self.warning_threshold,
            compact_threshold=self.compact_threshold,
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
        if self.max_vram_gb == 0.0:
            return (self._current_tokens + incoming_tokens) > self.max_tokens
        if self.max_kv_cache_gb <= 0 or self.max_tokens <= 0:
            return True
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
        weights_gb = model.compute_weights_gb()
        if hardware.vram_gb <= 0 or weights_gb <= 0:
            return None
        # If local weights exceed available VRAM (inference server is partial CPU offloading),
        # do not instantiate a 0-budget governor that falsely overflows on turn 1.
        if (hardware.vram_gb - weights_gb - 0.5) <= 0:
            return None

        arch_val = getattr(getattr(model, "architecture", None), "value", getattr(model, "architecture", ""))
        return KVCacheGovernor(
            max_vram_gb=hardware.vram_gb,
            model_weights_gb=weights_gb,
            kv_per_token_mb=model.kv_per_token_mb,
            kv_cache_quant=runtime.kv_cache_quant,
            is_moe=str(arch_val).lower() == "moe",
        )
    except Exception as e:
        logger.debug(f"KVCacheGovernor: failed to create for {model_name}: {e}")
        return None


def get_or_create_governor_for_orchestrator(
    orchestrator: object,
    model_name: Optional[str],
    state: Optional[dict] = None,
) -> Optional[KVCacheGovernor]:
    """Return a cached KVCacheGovernor on *orchestrator*, resetting post-compaction."""
    if not model_name:
        return None
    gov = getattr(orchestrator, "_kv_governor", None) if orchestrator is not None else None
    cached_model = getattr(orchestrator, "_kv_governor_model", None) if orchestrator is not None else None
    if gov is None or cached_model != model_name:
        gov = create_governor_for_model(model_name)
        if orchestrator is not None:
            try:
                setattr(orchestrator, "_kv_governor", gov)
                setattr(orchestrator, "_kv_governor_model", model_name)
                setattr(orchestrator, "_kv_governor_compactions", 0)
            except Exception:
                pass
    if gov is not None and state is not None and orchestrator is not None:
        comp_cnt = int((state.get("run") or {}).get("compactions") or 0)
        seen_cnt = int(getattr(orchestrator, "_kv_governor_compactions", 0) or 0)
        if comp_cnt > seen_cnt:
            gov.reset()
            try:
                setattr(orchestrator, "_kv_governor_compactions", comp_cnt)
            except Exception:
                pass
    return gov


def reset_governor_for_orchestrator(orchestrator: Optional[object]) -> None:
    """Reset the active KVCacheGovernor attached to *orchestrator* after compaction."""
    gov = getattr(orchestrator, "_kv_governor", None) if orchestrator is not None else None
    if isinstance(gov, KVCacheGovernor):
        gov.reset()


def create_governor_from_gguf(
    gguf_path: str | object,
    *,
    max_vram_gb: float,
    os_name: str = "darwin",
    kv_quant: str = "auto",
) -> Optional[KVCacheGovernor]:
    """Create an exact ``KVCacheGovernor`` from a local GGUF file header (TASK-35)."""
    from pathlib import Path
    from src.core.inference.gguf_header import (
        estimate_gguf_memory_fit,
        read_gguf_header,
    )

    info = read_gguf_header(Path(str(gguf_path)))
    fit = estimate_gguf_memory_fit(
        info,
        total_memory_gb=max_vram_gb,
        os_name=os_name,
        kv_quant=kv_quant,
    )
    if not fit.fits or fit.kv_mb_per_1k_tokens <= 0:
        return None
    # Pass raw f16 kv_per_token_mb and let KVCacheGovernor apply kv_quant scaling?
    # Better: pass exact quantized kv_mb_per_1k_tokens directly with kv_cache_quant=False
    # so the governor matches GgufMemoryFit to the exact byte without double-scaling.
    return KVCacheGovernor(
        max_vram_gb=fit.available_memory_gb,
        model_weights_gb=fit.weight_gb,
        kv_per_token_mb=fit.kv_mb_per_1k_tokens,
        overhead_gb=fit.os_reserve_gb + fit.compute_buffer_gb,
        kv_cache_quant=False,
    )
