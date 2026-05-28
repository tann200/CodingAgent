"""Startup health checks for providers and models.

Provides a reusable provider health check that can be called from `main.py` or tests.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict

from src.core.inference.llm_manager import get_provider_manager
from src.core.logger import logger as guilogger
from src.core.utils.strings import valid_str as _valid_str, extract_str as _extract_str


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
        # Skip health check for explicitly inactive providers. Prefer reading
        # the providers.json config via load_provider so we don't rely on
        # adapter.provider being present. Fall back to adapter.provider only
        # if providers.json cannot be read.
        try:
            from src.core.inference.llm_manager import (
                load_provider,
                canonical_provider,
            )

            # Conservative validators to avoid consuming test MagicMock placeholders
            def _extract_name(p: Any) -> str:
                if isinstance(p, str):
                    return p
                if isinstance(p, dict):
                    return p.get("name") or p.get("type") or p.get("key") or ""
                return ""

            raw_cfg = load_provider(pm.providers_config_path)
            providers_cfg = (
                raw_cfg
                if isinstance(raw_cfg, list)
                else ([raw_cfg] if isinstance(raw_cfg, dict) else [])
            )
            skip = False
            for p in providers_cfg:
                try:
                    cand = _extract_name(p)
                    if not _valid_str(cand):
                        continue
                    k = canonical_provider(cand)
                    if k == key and p.get("active") is False:
                        skip = True
                        break
                except Exception:
                    continue
            if skip:
                continue
        except Exception:
            # Fall back to adapter metadata if providers.json cannot be read
            try:
                if adapter and isinstance(getattr(adapter, "provider", None), dict):
                    if adapter.provider.get("active") is False:
                        continue
            except Exception:
                # If adapter metadata access fails, do not skip — perform health check
                pass
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
                    _raw = adapter.get_models_from_api()
                    if inspect.isawaitable(_raw):
                        models_resp = await asyncio.wait_for(_raw, timeout=timeout)
                    else:
                        models_resp = _raw
                except asyncio.TimeoutError:
                    res["error"] = f"timeout after {timeout}s"
                    guilogger.warning(
                        f"Startup: provider '{key}' timed out after {timeout}s"
                    )
                    results[key] = res
                    continue
                except Exception as exc:
                    # CODE_QUALITY_AUDIT #8 fix: log context before re-raising so the
                    # caller's traceback includes the provider name and exception message.
                    # A bare `raise` here swallowed all diagnostic context.
                    guilogger.error(
                        f"Startup: provider '{key}' raised unexpected error during model fetch: {exc}"
                    )
                    raise

                if isinstance(models_resp, dict):
                    models = models_resp.get("models") or []
                else:
                    models = (
                        models_resp
                        if isinstance(models_resp, (list, tuple))
                        else [models_resp]
                    )

                # Normalize and conservatively filter model entries
                norm: list = []
                try:
                    for m in models:
                        cand = _extract_str(m)  # type: ignore[assignment]
                        if cand and _valid_str(cand):
                            norm.append(cand)
                except Exception:
                    norm = []

                res["models"] = norm
                res["ok"] = bool(norm)
                if not norm:
                    guilogger.warning(
                        f"Startup: provider '{key}' returned no models from API or models were filtered"
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
