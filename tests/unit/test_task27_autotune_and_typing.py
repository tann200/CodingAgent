"""TASK-27 tests: zero-config local runtime auto-tuning + doctor integration."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch


def test_compute_safe_local_defaults_scales_by_memory_tier() -> None:
    from src.core.inference.local_runtime_autotune import compute_safe_local_defaults

    # 16 GB Apple Silicon unified memory -> 32k n_ctx, q8_0, compact-flat
    m16 = compute_safe_local_defaults(
        ram_gb=16.0, vram_gb=0.0, os_name="darwin", model="qwen3.5:9b"
    )
    assert m16["effective_accelerator_gb"] == 16.0
    assert m16["headroom_gb"] == 12.0
    assert m16["recommended_n_ctx"] == 32768
    assert m16["kv_cache_quant"] == "q8_0"
    assert m16["recommended_toolset"] == "compact-flat"

    # 64 GB Apple Silicon with live llama-server n_ctx override
    m64 = compute_safe_local_defaults(
        ram_gb=64.0,
        vram_gb=0.0,
        os_name="darwin",
        model="qwen3-32b",
        live_n_ctx=49152,
    )
    assert m64["recommended_n_ctx"] == 49152
    assert m64["hardware_cap_n_ctx"] == 65536
    assert m64["kv_cache_quant"] == "f16"
    assert m64["recommended_toolset"] == "standard-flat"


def test_detect_local_runtime_profile_selects_live_endpoint() -> None:
    from src.core.inference.local_runtime_autotune import detect_local_runtime_profile

    def fake_probe(provider: str, url: str, timeout: float):
        if provider == "llama_server":
            return True, ["Qwen3.5-9B-Q4_K_M.gguf"], 32768, 4.2
        return False, [], None, None

    fake_hw = SimpleNamespace(ram_gb=24.0, vram_gb=0.0, os="darwin")
    with patch(
        "src.core.inference.local_runtime_autotune.detect_hardware",
        return_value=fake_hw,
    ):
        prof = detect_local_runtime_profile(probe_fn=fake_probe)

    assert len(prof["detected_endpoints"]) == 1
    assert prof["selected"]["provider"] == "llama_server"
    assert prof["selected"]["live_n_ctx"] == 32768
    assert prof["recommended"]["recommended_n_ctx"] == 32768
    assert prof["recommended"]["recommended_toolset"] == "compact-flat"


def test_doctor_diagnosis_includes_local_autotune_and_renders_cleanly(capsys) -> None:
    from src.cli.doctor_cmd import (
        build_doctor_diagnosis,
        format_doctor_tui_markup,
        run_doctor,
    )

    fake_providers = [
        {
            "provider": "llama_server",
            "online": True,
            "status": "healthy",
            "endpoint": "http://127.0.0.1:8080",
            "latency_ms": 3.5,
            "target_model": "qwen3.5:9b",
            "models": ["qwen3.5:9b"],
            "policy": {
                "tier": "compact",
                "toolset": "compact-flat",
                "tool_limit": 12,
                "context_window": 32768,
            },
            "tool_validation": {"schemas_valid": True, "schemas_count": 12},
            "recommendations": [],
        }
    ]

    with (
        patch("src.cli.doctor_cmd.load_providers_config", return_value=[{"name": "llama_server", "type": "llama_server"}]),
        patch("src.cli.doctor_cmd.diagnose_openai_compat_provider", return_value=fake_providers[0]),
    ):
        diag = build_doctor_diagnosis(SimpleNamespace(workdir=".", provider=None, model=None, endpoint=None, validate_tools=False, json=True))
        assert "local_autotune" in diag
        assert diag["local_autotune"]["selected"]["provider"] == "llama_server"
        assert diag["local_autotune"]["recommended"]["recommended_n_ctx"] == 32768

        markup = format_doctor_tui_markup(diag)
        assert "Auto-Tune:" in markup
        assert "compact-flat" in markup

        rc = run_doctor(SimpleNamespace(workdir=".", provider=None, model=None, endpoint=None, validate_tools=False, json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert "local_autotune" in out
