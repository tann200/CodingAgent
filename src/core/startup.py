"""Startup health checks for providers and models.

Provides a reusable provider health check that can be called from `main.py` or tests.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict

from src.core.inference.llm_manager import get_provider_manager
from src.core.logger import logger as guilogger


async def provider_health_check(timeout: float = 5.0) -> Dict[str, Dict[str, Any]]:
    """Check each registered provider for adapter presence and model availability.

    Returns a dict mapping provider_key -> {adapter_present: bool, ok: bool, models: list|None, error: str|None}
    """
    pm = get_provider_manager()
    if not pm._initialized:
        await pm.initialize()

    results: Dict[str, Dict[str, Any]] = {}

    for key in pm.list_providers():
        adapter = pm.get_provider(key)
        res: Dict[str, Any] = {
            "adapter_present": bool(adapter),
            "ok": False,
            "models": None,
            "error": None,
        }
        if not adapter:
            guilogger.warning(f"Startup: no adapter registered for provider '{key}'")
            res["error"] = "no_adapter"
            results[key] = res
            continue

        # Prefer adapter.get_models_from_api when available
        try:
            if hasattr(adapter, "get_models_from_api") and callable(
                getattr(adapter, "get_models_from_api")
            ):
                try:
                    models_resp = adapter.get_models_from_api()
                except Exception:
                    # Some adapters may expose async get_models_from_api
                    maybe = adapter.get_models_from_api
                    if inspect.isawaitable(maybe):
                        models_resp = await maybe()
                    else:
                        raise

                if isinstance(models_resp, dict):
                    models = models_resp.get("models") or []
                    # normalize list of dicts or strings
                    norm = []
                    for m in models:
                        if isinstance(m, dict):
                            name = m.get("name") or m.get("id") or m.get("key")
                            if name:
                                norm.append(name)
                        elif isinstance(m, str):
                            norm.append(m)
                    res["models"] = norm
                    res["ok"] = bool(norm)
                    if not norm:
                        guilogger.warning(
                            f"Startup: provider '{key}' returned no models from API"
                        )
                else:
                    # Unexpected shape - treat as not ok but include raw
                    res["models"] = models_resp
                    res["error"] = "unexpected_models_shape"
                    guilogger.warning(
                        f"Startup: provider '{key}' returned unexpected models shape: {type(models_resp)}"
                    )
            else:
                # Fallback: try validate_connection
                if hasattr(adapter, "validate_connection") and callable(
                    getattr(adapter, "validate_connection")
                ):
                    ok = adapter.validate_connection()
                    if inspect.isawaitable(ok):
                        ok = await ok
                    res["ok"] = bool(ok)
                    if not ok:
                        res["error"] = "validate_connection_failed"
                        guilogger.warning(
                            f"Startup: provider '{key}' validate_connection failed"
                        )
                else:
                    # No explicit checks available - mark as ok (best-effort)
                    res["ok"] = True
        except Exception as e:
            res["error"] = str(e)
            guilogger.error(f"Startup: failed to query provider '{key}': {e}")

        results[key] = res

    return results


def run_provider_health_check_sync(timeout: float = 5.0) -> Dict[str, Dict[str, Any]]:
    """Sync wrapper for provider_health_check to be called at app startup."""
    return asyncio.run(provider_health_check(timeout=timeout))
