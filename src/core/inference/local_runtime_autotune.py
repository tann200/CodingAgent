"""Zero-config local runtime detection and auto-tuning (TASK-27).

Probes common local inference runtimes (``llama-server``, ``mlx``, ``ollama``,
``lm_studio``), inspects hardware RAM/VRAM headroom via
``detect_hardware()``, and computes safe ``n_ctx``, ``kv_cache_quant``,
``ModelRuntimePolicy`` tier, and flat toolset recommendations without requiring
manual JSON edits.
"""

from __future__ import annotations

import platform
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from src.core.inference.hardware_capability_profile import detect_hardware
from src.core.orchestration.graph.runtime_policy import resolve_runtime_policy

# Ordered preference for local-first developer setups.
LOCAL_RUNTIME_ENDPOINTS: Tuple[Tuple[str, str, str], ...] = (
    ("llama_server", "http://127.0.0.1:8080", "/v1/models"),
    ("mlx", "http://127.0.0.1:8081", "/v1/models"),
    ("ollama", "http://127.0.0.1:11434", "/api/tags"),
    ("lm_studio", "http://127.0.0.1:1234", "/v1/models"),
)

ProbeFn = Callable[[str, str, float], Tuple[bool, List[str], Optional[int], Optional[float]]]


def _default_probe_endpoint(
    provider: str, base_url: str, timeout: float = 0.5
) -> Tuple[bool, List[str], Optional[int], Optional[float]]:
    """Probe *base_url* for reachability, loaded model IDs, and live ``n_ctx``."""
    clean = base_url.rstrip("/")
    path = "/api/tags" if provider == "ollama" else "/v1/models"
    t0 = time.perf_counter()
    try:
        resp = requests.get(f"{clean}{path}", timeout=timeout)
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if resp.status_code != 200:
            return False, [], None, latency_ms
        data = resp.json() if resp.content else {}
    except Exception:
        return False, [], None, None

    models: List[str] = []
    if provider == "ollama" and isinstance(data, dict):
        for item in data.get("models") or []:
            if isinstance(item, dict) and item.get("name"):
                models.append(str(item["name"]))
    elif isinstance(data, dict):
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))

    live_n_ctx: Optional[int] = None
    if provider == "llama_server":
        try:
            props_resp = requests.get(f"{clean}/props", timeout=timeout)
            if props_resp.status_code == 200:
                props = props_resp.json()
                if isinstance(props, dict):
                    gen = props.get("default_generation_settings") or {}
                    if isinstance(gen, dict) and gen.get("n_ctx"):
                        live_n_ctx = int(gen["n_ctx"])
        except Exception:
            pass

    return True, models, live_n_ctx, latency_ms


def compute_safe_local_defaults(
    *,
    ram_gb: float,
    vram_gb: float,
    os_name: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    live_n_ctx: Optional[int] = None,
) -> Dict[str, Any]:
    """Compute hardware-safe local inference defaults (``n_ctx``, KV quant, toolset)."""
    effective_mem_gb = max(
        float(vram_gb or 0.0),
        float(ram_gb or 0.0) if (os_name or "").lower() == "darwin" else 0.0,
    )
    # Reserve realistic OS/wired headroom (Apple Silicon unified memory needs ~25% OS headroom)
    os_reserve_gb = round(max(4.0, effective_mem_gb * 0.25), 1) if effective_mem_gb > 0 else 4.0
    headroom_gb = round(max(0.0, effective_mem_gb - os_reserve_gb), 1)

    if effective_mem_gb >= 32.0:
        hw_n_ctx = 65536
        kv_cache_quant = "f16"
        default_tier_model = "qwen3-32b"
    elif effective_mem_gb >= 16.0:
        hw_n_ctx = 32768
        kv_cache_quant = "q8_0"
        default_tier_model = "qwen3.5:9b"
    elif effective_mem_gb >= 8.0:
        hw_n_ctx = 16384
        kv_cache_quant = "q8_0"
        default_tier_model = "qwen3:8b"
    else:
        hw_n_ctx = 8192
        kv_cache_quant = "q4_0"
        default_tier_model = "qwen3:4b"

    target_model = model or default_tier_model
    effective_n_ctx = int(live_n_ctx) if (live_n_ctx and live_n_ctx > 0) else hw_n_ctx
    policy = resolve_runtime_policy(
        model=target_model,
        provider=provider or "local",
        context_window=effective_n_ctx,
    )

    return {
        "ram_gb": round(float(ram_gb or 0.0), 1),
        "vram_gb": round(float(vram_gb or 0.0), 1),
        "effective_accelerator_gb": round(effective_mem_gb, 1),
        "headroom_gb": headroom_gb,
        "recommended_n_ctx": effective_n_ctx,
        "hardware_cap_n_ctx": hw_n_ctx,
        "kv_cache_quant": kv_cache_quant,
        "policy_tier": policy.policy_tier,
        "recommended_toolset": policy.toolset_name,
        "tool_limit": policy.tool_limit,
        "verification_mode": policy.verification_mode,
        "parallel_reads": policy.max_parallel_reads,
    }


def detect_local_runtime_profile(
    *,
    timeout: float = 0.5,
    probe_fn: Optional[ProbeFn] = None,
    existing_provider_results: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Detect active local runtimes and return auto-tuned runtime recommendations.

    When *existing_provider_results* (from ``build_doctor_diagnosis``) is supplied
    and *probe_fn* is ``None``, reuses already-probed local endpoints so
    ``codingagent doctor`` does not double-probe localhost ports.
    """
    hw = detect_hardware()
    ram_gb = float(getattr(hw, "ram_gb", 0.0) or 0.0)
    vram_gb = float(getattr(hw, "vram_gb", 0.0) or 0.0)
    os_name = str(getattr(hw, "os", platform.system().lower()))

    detected: List[Dict[str, Any]] = []
    if existing_provider_results is not None and probe_fn is None:
        local_names = {p for p, _, _ in LOCAL_RUNTIME_ENDPOINTS}
        for res in existing_provider_results:
            pname = str(res.get("provider") or "").lower().replace("-", "_")
            if pname in local_names and res.get("online"):
                models = list(res.get("models") or [])
                if not models and res.get("target_model"):
                    models = [str(res["target_model"])]
                pol = res.get("policy") or {}
                live_ctx = pol.get("context_window") if isinstance(pol, dict) else None
                detected.append(
                    {
                        "provider": pname,
                        "endpoint": res.get("endpoint") or "",
                        "models": models,
                        "live_n_ctx": int(live_ctx) if live_ctx else None,
                        "latency_ms": res.get("latency_ms"),
                    }
                )
    else:
        fn = probe_fn or _default_probe_endpoint
        for pname, url, _ in LOCAL_RUNTIME_ENDPOINTS:
            online, models, live_n_ctx, latency_ms = fn(pname, url, timeout)
            if online:
                detected.append(
                    {
                        "provider": pname,
                        "endpoint": url,
                        "models": models,
                        "live_n_ctx": live_n_ctx,
                        "latency_ms": latency_ms,
                    }
                )

    selected = detected[0] if detected else None
    sel_model = (
        selected["models"][0]
        if (selected and selected.get("models"))
        else None
    )
    recommended = compute_safe_local_defaults(
        ram_gb=ram_gb,
        vram_gb=vram_gb,
        os_name=os_name,
        model=sel_model,
        provider=selected["provider"] if selected else None,
        live_n_ctx=selected.get("live_n_ctx") if selected else None,
    )

    return {
        "detected_endpoints": detected,
        "selected": selected,
        "recommended": recommended,
    }


def select_local_autotune_provider(
    available_providers: List[str],
    *,
    timeout: float = 0.25,
    probe_fn: Optional[ProbeFn] = None,
) -> Optional[str]:
    """Return the highest-priority reachable local provider registered in *available_providers*.

    Used as a zero-config startup fallback when no explicit provider is active.
    Never blocks for long (default 250ms per local endpoint) and returns ``None``
    when no local engine is online.
    """
    if not available_providers:
        return None
    avail_norm = {
        str(p).lower().replace("-", "_"): str(p) for p in available_providers
    }
    fn = probe_fn or _default_probe_endpoint
    for pname, url, _ in LOCAL_RUNTIME_ENDPOINTS:
        if pname not in avail_norm:
            continue
        try:
            online, _, _, _ = fn(pname, url, timeout)
            if online:
                return avail_norm[pname]
        except Exception:
            continue
    return None


__all__ = [
    "LOCAL_RUNTIME_ENDPOINTS",
    "compute_safe_local_defaults",
    "detect_local_runtime_profile",
    "select_local_autotune_provider",
]
