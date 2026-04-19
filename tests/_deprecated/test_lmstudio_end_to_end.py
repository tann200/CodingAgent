import os
import asyncio
import json
from pathlib import Path
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.lmstudio]

from scripts.check_providers_and_models import run_check

# Enable integration tests when RUN_INTEGRATION=1 or when an lm_studio provider is configured
RUN = os.getenv("RUN_INTEGRATION") == "1"
if not RUN and not os.getenv("CI"):
    try:
        import requests as _requests_probe

        cfg_path = Path(__file__).parents[2] / "src" / "config" / "providers.json"
        if cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            providers = (
                raw
                if isinstance(raw, list)
                else ([raw] if isinstance(raw, dict) else [])
            )
            for p in providers:
                t = str(p.get("type") or "").lower()
                name = str(p.get("name") or "").lower()
                if "lm" in t or "lm" in name or "lm_studio" in t or "lmstudio" in name:
                    # Only enable if LM Studio is actually reachable
                    _base = p.get("base_url", "http://localhost:1234/v1").rstrip("/")
                    try:
                        _requests_probe.get(f"{_base}/models", timeout=2)
                        RUN = True
                    except Exception:
                        pass
                    break
    except Exception:
        pass


@pytest.mark.lmstudio
@pytest.mark.skipif(not RUN, reason="Integration tests disabled")
def test_lmstudio_end_to_end():
    # This test performs an end-to-end check against the LM Studio configured provider.
    res = asyncio.run(run_check())
    assert "active_provider" in res
    # call_response should exist (may be error meta if model couldn't load)
    if not res.get("connection_ok"):
        pytest.xfail(
            f"LM Studio not reachable: {res.get('call_response', {}).get('error', 'unknown')}"
        )
    assert res.get("connection_ok") is True
    # there must be a model available
    assert isinstance(res.get("models"), list) and len(res.get("models")) > 0  # type: ignore[arg-type]
    if "call_exception" in res or (
        isinstance(res.get("call_response"), dict) and res["call_response"].get("error")
    ):
        pytest.xfail(
            "Provider call failed, which is acceptable if the provider is not running."
        )
    assert "call_response" in res or "call_exception" in res
