"""Check configured providers and models, then run a simple prompt against the active provider.

Usage:
  python scripts/check_providers_and_models.py

Environment:
  RUN_INTEGRATION=1 to run integration-style checks (will actually call providers)

This script is safe to run locally and prints JSON-style diagnostics.
"""

from __future__ import annotations

import os
import json
import asyncio
from pathlib import Path
from typing import Any

from src.core.inference.llm_manager import (
    get_provider_manager,
    get_available_models,
    call_model,
    canonical_provider,
)
from src.core.logger import logger as guilogger


async def run_check(provider_override: str | None = None):
    pm = get_provider_manager()
    await pm.initialize()
    providers = pm.list_providers()
    out: dict[str, Any] = {"providers": providers}

    # choose active provider
    p_name = provider_override or (os.getenv("ACTIVE_PROVIDER") or "lm_studio")
    p_key = canonical_provider(p_name)
    out["active_provider"] = p_key

    adapter = pm.get_provider(p_key)
    if adapter is None:
        out["error"] = f"provider_not_configured: {p_key}"
        print(json.dumps(out, indent=2))
        return out

    # validate connection
    try:
        ok = await pm.validate_provider(p_key)
    except Exception as e:
        ok = False
        guilogger.error(f"validate_provider threw: {e}")
    out["connection_ok"] = bool(ok)

    # list available models (full ids preferred)
    try:
        models = await get_available_models("", "", p_key)
    except Exception as e:
        models = []
        guilogger.error(f"get_available_models threw: {e}")
    out["models"] = models

    # if no models, advise user
    if not models:
        out["advice"] = (
            "No models found for provider. Check that the provider service is running, "
            "that the provider base_url in src/config/providers.json is correct, and that a model is loaded."
        )
        print(json.dumps(out, indent=2))
        return out

    # pick model (prefer configured model in provider config)
    configured = None
    try:
        pmeta = getattr(adapter, "provider", {}) or {}
        pmodels = pmeta.get("models") if isinstance(pmeta, dict) else None
        if pmodels:
            # provider config likely contains full id strings
            if isinstance(pmodels, list) and pmodels:
                first = pmodels[0]
                configured = (
                    first
                    if isinstance(first, str)
                    else (first.get("id") or first.get("key") or first.get("name"))
                )
    except Exception:
        configured = None

    out["configured_model"] = configured
    chosen = configured or models[0]
    out["chosen_model"] = chosen

    # run a simple chat with system prompt from agent-brain
    system_prompt_file = Path("agent-brain") / "system_prompt_coding.md"
    system_prompt = (
        system_prompt_file.read_text(encoding="utf-8")
        if system_prompt_file.exists()
        else "You are a helpful local coding assistant."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Explain why the sky is blue in 2-3 sentences."},
    ]

    # perform a single call
    try:
        resp = await call_model(
            messages, provider=p_key, model=chosen, stream=False, format_json=False
        )
        out["call_response"] = resp
    except Exception as e:
        out["call_exception"] = str(e)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return out


if __name__ == "__main__":
    # allow override provider via env var
    prov = os.getenv("ACTIVE_PROVIDER")
    asyncio.run(run_check(provider_override=prov))
