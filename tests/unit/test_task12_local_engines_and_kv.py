from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_ollama_adapter_forwards_num_ctx_and_kv_cache_quant(monkeypatch):
    from src.core.inference.adapters.ollama_adapter import OllamaAdapter

    adapter = OllamaAdapter(
        base_url="http://localhost:11434/api",
        models=["qwen3.5:9b"],
    )
    adapter.provider = {"name": "ollama", "type": "ollama", "context_length": 32768, "kv_cache_quant": "q8_0"}

    captured_payloads: list[dict] = []

    def _fake_post(url, payload, **_kw):
        captured_payloads.append(payload)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "model": "qwen3.5:9b",
            "message": {"role": "assistant", "content": "ok"},
            "prompt_eval_count": 10,
            "eval_count": 5,
        }
        return resp

    monkeypatch.setattr(adapter, "_post_generation_with_retry", _fake_post)
    out = adapter.generate([{"role": "user", "content": "hi"}], model="qwen3.5:9b")
    assert out["ok"] is True
    assert len(captured_payloads) == 1
    opts = captured_payloads[0].get("options") or {}
    assert opts["num_ctx"] == 32768
    assert opts["kv_cache_type"] == "q8_0"

    # Explicit num_ctx + kv_cache_quant override
    captured_payloads.clear()
    adapter.generate(
        [{"role": "user", "content": "hi"}],
        model="qwen3.5:9b",
        num_ctx=16384,
        kv_cache_quant="q4_0",
    )
    opts2 = captured_payloads[0]["options"]
    assert opts2["num_ctx"] == 16384
    assert opts2["kv_cache_type"] == "q4_0"
    assert "kv_cache_quant" not in captured_payloads[0]


def test_metal_vram_reserves_os_headroom_and_kv_governor_moe_quant(monkeypatch):
    from src.core.inference import hardware_capability_profile as hcp
    from src.core.inference.model_capability_profile import get_model_profile
    from src.core.inference.kv_cache_governor import (
        KVCacheGovernor,
        create_governor_for_model,
        get_or_create_governor_for_orchestrator,
        reset_governor_for_orchestrator,
    )

    monkeypatch.setattr(
        hcp.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=str(24 * (1024**3))),
    )
    metal_gb = hcp._detect_vram_metal()
    assert metal_gb == pytest.approx(18.0, rel=0.01)  # 75% of 24GB

    gemma_moe = get_model_profile("gemma-4-26b-a4b")
    assert gemma_moe.compute_kv_cache_gb(1000, kv_cache_quant="q8_0") == pytest.approx(
        gemma_moe.compute_kv_cache_gb(1000) * 0.5
    )

    # KVCacheGovernor quant + MoE scaling
    gov_dense = KVCacheGovernor(max_vram_gb=16.0, model_weights_gb=10.0, kv_per_token_mb=1.6, is_moe=False)
    gov_moe = KVCacheGovernor(max_vram_gb=16.0, model_weights_gb=10.0, kv_per_token_mb=1.6, is_moe=True)
    assert gov_moe.kv_per_token_mb < 1.0
    assert gov_moe.max_tokens > gov_dense.max_tokens

    gov_fp16 = KVCacheGovernor(max_vram_gb=16.0, model_weights_gb=10.0, kv_per_token_mb=1.0, kv_cache_quant=False)
    gov_q8 = KVCacheGovernor(max_vram_gb=16.0, model_weights_gb=10.0, kv_per_token_mb=1.0, kv_cache_quant=True)
    assert gov_q8.max_tokens >= gov_fp16.max_tokens * 2 - 2

    # Overcommitted weights on local GPU must return None from create_governor_for_model (no false turn-1 overflow)
    fake_runtime = SimpleNamespace(
        is_cloud=False,
        model=SimpleNamespace(compute_weights_gb=lambda: 20.0, kv_per_token_mb=1.0),
        hardware=SimpleNamespace(vram_gb=16.0),
        kv_cache_quant=False,
    )
    monkeypatch.setattr("src.core.inference.get_runtime_profile", lambda *_a, **_kw: fake_runtime)
    assert create_governor_for_model("large-model") is None

    # Orchestrator governor persists & resets post-compaction
    orch = SimpleNamespace()
    monkeypatch.setattr(
        "src.core.inference.kv_cache_governor.create_governor_for_model",
        lambda _m: KVCacheGovernor(max_vram_gb=16.0, model_weights_gb=6.0, kv_per_token_mb=1.0),
    )
    gov = get_or_create_governor_for_orchestrator(orch, "qwen3:9b")
    assert gov is not None
    gov.on_context_update(5000)
    assert gov._current_tokens == 5000
    reset_governor_for_orchestrator(orch)
    assert gov._current_tokens == 0
    assert gov._compaction_count == 1


def test_mlx_and_llama_server_adapters_and_discovery(monkeypatch):
    from src.core.inference.adapters.mlx_adapter import MlxAdapter, discover_mlx_server
    from src.core.inference.adapters.llama_server_adapter import LlamaServerAdapter, discover_llama_server
    from src.core.inference.provider_loading import resolve_adapter_class

    cls_mlx, err1 = resolve_adapter_class(provider_type="mlx", camelize=lambda s: "".join(w.capitalize() for w in s.split("_")))
    cls_llama, err2 = resolve_adapter_class(provider_type="llama_server", camelize=lambda s: "".join(w.capitalize() for w in s.split("_")))
    assert err1 is None and cls_mlx is MlxAdapter
    assert err2 is None and cls_llama is LlamaServerAdapter

    mlx = MlxAdapter(models=["mlx-community/Qwen2.5-Coder-7B-4bit"])
    p_mlx, ep_mlx = mlx._build_payload(
        [{"role": "user", "content": "hi"}],
        "mlx-community/Qwen2.5-Coder-7B-4bit",
        {"num_ctx": 32768, "kv_cache_quant": "q8_0"},
    )
    assert ep_mlx == "http://localhost:8080/v1/chat/completions"
    assert p_mlx["kv_bits"] == 8
    assert p_mlx["max_kv_size"] == 32768

    llama = LlamaServerAdapter(models=["qwen2.5-coder.gguf"])
    p_llama, ep_llama = llama._build_payload(
        [{"role": "user", "content": "hi"}],
        "qwen2.5-coder.gguf",
        {"num_ctx": 32768, "kv_cache_quant": True},
    )
    assert ep_llama == "http://localhost:8080/v1/chat/completions"
    assert p_llama["cache_prompt"] is True
    assert "num_ctx" not in p_llama and "kv_cache_quant" not in p_llama

    monkeypatch.setattr(
        "src.core.inference.adapters.llama_server_adapter.requests.get",
        lambda url, **_kw: SimpleNamespace(
            status_code=200,
            json=lambda: (
                {"default_generation_settings": {"n_ctx": 65536}, "model_path": "/models/qwen3.gguf"}
                if url.endswith("/props")
                else {"data": []}
            ),
        ),
    )
    info = discover_llama_server("http://localhost:8080/v1")
    assert info["available"] is True
    assert info["n_ctx"] == 65536
    assert info["models"] == ["qwen3.gguf"]

    monkeypatch.setattr(
        "src.core.inference.adapters.mlx_adapter.requests.get",
        lambda url, **_kw: SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"id": "mlx-community/gemma-4"}]},
        ),
    )
    mlx_info = discover_mlx_server("http://localhost:8080/v1")
    assert mlx_info["available"] is True
    assert mlx_info["models"] == ["mlx-community/gemma-4"]
