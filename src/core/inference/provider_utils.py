"""Shared provider resolution utilities.

Provides a single implementation of the 3-tier provider/model resolution cascade
used throughout the codebase. This eliminates duplication between inference_loop.py
and perception_node.py.

Resolution priority:
  1) orchestrator.get_provider_capabilities() (authoritative)
  2) ProviderManager.get_provider_capabilities(adapter)
  3) adapter attributes (provider, default_model, models)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    pass


def _extract_str(value: Any) -> Optional[str]:
    """Extract a string from a value, returning None on failure."""
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    try:
        return str(value)
    except Exception:
        return None


def resolve_provider_capabilities(
    orchestrator: Any, adapter: Any = None
) -> dict[str, Any]:
    """Resolve provider capabilities using the 3-tier cascade.

    Returns a dict with keys:
      - provider_name: str or None
      - model: str or None
      - supports_native_tools: bool
      - provider_family: str (defaults to "default")

    All imports are local to avoid circular import issues.
    """
    caps: dict[str, Any] = {}

    explicit_model = None
    explicit_provider = None
    try:
        explicit_model = _extract_str(getattr(orchestrator, "model", None))
    except Exception:
        explicit_model = None
    try:
        explicit_provider = _extract_str(getattr(orchestrator, "_provider_name", None))
    except Exception:
        explicit_provider = None

    # 1) Orchestrator-level capabilities (authoritative)
    try:
        if (
            orchestrator
            and hasattr(orchestrator, "get_provider_capabilities")
            and callable(getattr(orchestrator, "get_provider_capabilities"))
        ):
            _rc = orchestrator.get_provider_capabilities()
            if isinstance(_rc, dict) and _rc:
                caps = dict(_rc)
    except Exception:
        caps = {}

    # 2) ProviderManager fallback
    if not caps:
        try:
            from src.core.inference.llm_manager import get_provider_manager as _gpm

            _pm = _gpm()
            if _pm is not None and adapter is not None:
                _rc = _pm.get_provider_capabilities(adapter)
                if isinstance(_rc, dict) and _rc:
                    caps = dict(_rc)
        except Exception:
            pass

    # 3) Adapter-only last resort (no network probes)
    if not caps and adapter is not None:
        try:
            prov_attr = getattr(adapter, "provider", None)
        except Exception:
            prov_attr = None

        provider_name: Optional[str] = None
        try:
            provider_name = _extract_str(prov_attr)
        except Exception:
            provider_name = None
        if not provider_name:
            try:
                provider_name = _extract_str(getattr(adapter, "name", None))
            except Exception:
                provider_name = None

        model: Optional[str] = None
        try:
            model = _extract_str(getattr(adapter, "default_model", None))
        except Exception:
            model = None
        if not model:
            try:
                models_attr = getattr(adapter, "models", None)
                if isinstance(models_attr, (list, tuple)):
                    for m in models_attr:
                        mm = _extract_str(m)
                        if mm:
                            model = mm
                            break
                else:
                    model = _extract_str(models_attr)
            except Exception:
                model = None

        supports_native_tools = False
        try:
            if isinstance(prov_attr, dict):
                supports_native_tools = bool(prov_attr.get("supports_native_tools", False))
            else:
                supports_native_tools = bool(getattr(adapter, "supports_native_tools", False))
        except Exception:
            supports_native_tools = False

        provider_family = "default"
        try:
            from src.core.orchestration.provider_capabilities import (
                _map_provider_family_impl as _map_pf,
            )

            provider_family = _map_pf(provider_name or "")
        except Exception:
            provider_family = "default"

        caps = {
            "supports_native_tools": bool(supports_native_tools),
            "provider_family": provider_family,
            "model": model,
            "provider_name": provider_name or "",
        }

    # Sanitize and normalize final values
    try:
        _pname = _extract_str(
            caps.get("provider_name") or caps.get("provider") or caps.get("name")
        )
    except Exception:
        _pname = None
    try:
        _model = _extract_str(caps.get("model") or caps.get("default_model"))
    except Exception:
        _model = None

    resolved = {
        "supports_native_tools": bool(caps.get("supports_native_tools", False)),
        "provider_family": caps.get("provider_family") or "default",
        "model": explicit_model or _model,
        "provider_name": explicit_provider or _pname,
    }
    if resolved["provider_family"] == "default" and resolved["provider_name"]:
        try:
            from src.core.orchestration.provider_capabilities import (
                _map_provider_family_impl as _map_pf,
            )

            resolved["provider_family"] = _map_pf(resolved["provider_name"])  # type: ignore[arg-type]
        except Exception:
            pass
    return resolved


def resolve_provider_and_model(orchestrator: Any) -> tuple[Optional[str], Optional[str]]:
    """Simple provider/model tuple resolution for fallback LLM calls.

    Returns (provider_name, model_name) — mirrors the legacy interface in inference_loop.py.
    """
    caps = resolve_provider_capabilities(orchestrator)
    return caps.get("provider_name"), caps.get("model")
