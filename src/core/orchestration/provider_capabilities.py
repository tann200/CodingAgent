"""Provider family mapping and capability detection.

Extracted from orchestrator.py (Phase G3) — single responsibility.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.core.utils.strings import valid_str as _valid_str, extract_str as _extract_str  # noqa: F401 (re-exported)


# S6-B: Provider family → prompt partial selection
_PROVIDER_FAMILY_MAP: Dict[str, str] = {
    # Anthropic
    "anthropic": "anthropic",
    # OpenAI-compatible cloud
    "openai": "openai",
    "openrouter": "openai",
    "github_copilot": "openai",
    "copilot": "openai",
    # Local / self-hosted
    "ollama": "local",
    "lm_studio": "local",
    "lmstudio": "local",
    "local": "local",
    # Test mock
    "mock": "mock",
}


def _map_provider_family_impl(raw_name: str) -> str:
    """Map a provider name/type string to a canonical family string.

    Matching is case-insensitive and checks for substring containment so
    that variant spellings (e.g. "lm-studio", "LMStudio") are captured.
    Returns ``"default"`` when no known family matches.
    """
    normalised = raw_name.lower().replace("-", "_").replace(" ", "_")
    # Exact lookup first (fast path)
    if normalised in _PROVIDER_FAMILY_MAP:
        return _PROVIDER_FAMILY_MAP[normalised]
    # Substring scan (handles composite names like "anthropic-vertex")
    for key, family in _PROVIDER_FAMILY_MAP.items():
        if key in normalised:
            return family
    return "default"




def get_provider_capabilities_impl(orch: Any) -> Dict[str, Any]:
    """Get provider capabilities including supports_native_tools and provider_family.

    S6-B: Enriched to return ``provider_family`` so that
    ``ContextBuilder._select_prompt_partial()`` can choose the right
    provider-specific prompt partial without any special-casing outside
    this method.

    Capability keys returned:
      - ``supports_native_tools`` (bool)
      - ``provider_family`` (str)  — one of "anthropic", "openai", "local",
        "mock", "default"
      - ``model`` (str | None)     — active model name if known
      - ``provider_name`` (str)    — raw provider name for debugging
    """
    capabilities: Dict[str, Any] = {
        "supports_native_tools": False,
        "provider_family": "default",
        "model": None,
        "provider_name": "",
    }
    explicit_model = _extract_str(getattr(orch, "model", None)) if orch else None
    explicit_provider = _extract_str(getattr(orch, "_provider_name", None)) if orch else None
    try:
        # Prefer central ProviderManager when available so callers obtain a
        # consistent capability view. Sanitize any returned values so tests
        # that use MagicMock placeholders don't leak into production fields.
        try:
            from src.core.inference.llm_manager import get_provider_manager

            mgr = get_provider_manager()
            adapter = getattr(orch, "_adapter", None)
            caps = mgr.get_provider_capabilities(adapter or None)
            if isinstance(caps, dict) and caps:
                provider_name = _extract_str(
                    caps.get("provider_name")
                    or caps.get("provider")
                    or caps.get("name")
                )
                model = _extract_str(caps.get("model") or caps.get("default_model"))
                provider_family = (
                    caps.get("provider_family")
                    if _valid_str(caps.get("provider_family") or "")
                    else None
                )
                if not provider_family and provider_name:
                    provider_family = _map_provider_family_impl(provider_name)
                provider_family = provider_family or "default"
                sanitized = {
                    "supports_native_tools": bool(
                        caps.get("supports_native_tools", False)
                    ),
                    "provider_family": provider_family,
                    "model": explicit_model or model,
                    "provider_name": explicit_provider or provider_name or "",
                }
                if explicit_provider:
                    sanitized["provider_family"] = _map_provider_family_impl(
                        sanitized["provider_name"]
                    )
                elif sanitized["provider_family"] == "default" and sanitized["provider_name"]:
                    sanitized["provider_family"] = _map_provider_family_impl(
                        sanitized["provider_name"]
                    )
                return sanitized
        except Exception:
            # Fall back to local implementation below when ProviderManager is
            # not available (eg. in certain test setups).
            pass

        if not getattr(orch, "_adapter", None):
            return capabilities

        # ── 1. Extract raw name strings ────────────────────────────────
        raw_name: Optional[str] = None
        raw_type: Optional[str] = None

        provider_attr = getattr(orch._adapter, "provider", None)
        if isinstance(provider_attr, dict):
            raw_name = _extract_str(provider_attr.get("name"))
            raw_type = _extract_str(provider_attr.get("type"))
            capabilities["supports_native_tools"] = bool(
                provider_attr.get("supports_native_tools", False)
            )

        # Fall back to adapter.name (present on many adapters)
        if not raw_name:
            raw_name = _extract_str(getattr(orch._adapter, "name", None))

        # ── 2. Resolve provider_family ─────────────────────────────────
        lookup_str = raw_type or raw_name or ""
        family = _map_provider_family_impl(lookup_str) if lookup_str else "default"
        # If type didn't resolve, try name as well
        if family == "default" and raw_type and raw_name:
            family = _map_provider_family_impl(raw_name)

        capabilities["provider_family"] = family
        capabilities["provider_name"] = explicit_provider or raw_name or raw_type or ""

        # ── 3. Active model ────────────────────────────────────────────
        active_model = getattr(orch._adapter, "default_model", None)
        capabilities["model"] = explicit_model or _extract_str(active_model)

        # ── 4. GitHub Copilot model-name override ──────────────────────
        if family == "openai" and capabilities["model"]:
            m_lower = capabilities["model"].lower()
            if "claude" in m_lower:
                capabilities["provider_family"] = "anthropic"
            elif "gemini" in m_lower:
                capabilities["provider_family"] = "gemini"

    except Exception:
        pass
    return capabilities


def resolve_provider_capabilities(orchestrator: Any) -> Dict[str, Any]:
    """Full 3-tier provider capabilities resolution used by all graph nodes.

    Tier 1: orchestrator.get_provider_capabilities() — authoritative if present.
    Tier 2: ProviderManager.get_provider_capabilities(adapter) — manager fallback.
    Tier 3: Direct adapter attribute inspection — last resort, no network probes.

    Returns a dict with keys: supports_native_tools, provider_family, model,
    provider_name, provider_supports_parallel_tools, supports_function_call,
    supports_streaming.
    """
    provider_capabilities: Dict[str, Any] = {}
    explicit_model = _extract_str(getattr(orchestrator, "model", None)) if orchestrator else None
    explicit_provider = _extract_str(getattr(orchestrator, "_provider_name", None)) if orchestrator else None
    try:
        caps: Dict[str, Any] = {}

        # Tier 0: Read directly from providers.json when explicit_provider is set.
        # This covers the common case where the orchestrator has _provider_name set
        # (e.g. via CLI --provider flag) but the adapter was not registered with
        # ProviderManager (e.g. because ProviderManager initialised before the
        # provider was selected).
        # We record the value here and apply it as an authoritative override after
        # all other tiers, because Tier 1 (orchestrator.get_provider_capabilities)
        # may return a stale False when _adapter is None.
        _tier0_supports_native_tools: Optional[bool] = None
        if explicit_provider:
            try:
                import json as _json
                from pathlib import Path as _Path

                _cfg = _Path(__file__).parents[2] / "config" / "providers.json"
                if _cfg.exists():
                    _raw = _json.loads(_cfg.read_text(encoding="utf-8"))
                    _entries = _raw if isinstance(_raw, list) else [_raw]
                    for _entry in _entries:
                        if not isinstance(_entry, dict):
                            continue
                        _ptype = (_entry.get("type") or "").lower()
                        _pname = (_entry.get("name") or "").lower()
                        _ep_norm = explicit_provider.lower().replace("-", "_")
                        if _ptype == _ep_norm or _pname == _ep_norm:
                            _tier0_supports_native_tools = bool(
                                _entry.get("supports_native_tools", False)
                            )
                            break
            except Exception:
                pass

        # Tier 1: orchestrator-level (authoritative)
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

        # Tier 2: ProviderManager fallback
        if not caps:
            try:
                from src.core.inference.llm_manager import get_provider_manager as _gpm

                _pm = _gpm()
                adapter = getattr(orchestrator, "_adapter", None)
                _rc = _pm.get_provider_capabilities(adapter)
                if isinstance(_rc, dict) and _rc:
                    caps = dict(_rc)
            except Exception:
                caps = caps or {}

        # Tier 3: Adapter attribute inspection
        if not caps and orchestrator and getattr(orchestrator, "_adapter", None):
            adapter = orchestrator._adapter
            try:
                prov_attr = getattr(adapter, "provider", None)
            except Exception:
                prov_attr = None
            provider_name = _extract_str(prov_attr) or _extract_str(
                getattr(adapter, "name", None)
            )

            # model: prefer default_model, then first entry of models list
            model = _extract_str(getattr(adapter, "default_model", None))
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
                    supports_native_tools = bool(
                        prov_attr.get("supports_native_tools", False)
                    )
                else:
                    supports_native_tools = bool(
                        getattr(adapter, "supports_native_tools", False)
                    )
            except Exception:
                supports_native_tools = False

            provider_family = _map_provider_family_impl(provider_name or "")
            caps = {
                "supports_native_tools": supports_native_tools,
                "provider_family": provider_family,
                "model": explicit_model or model,
                "provider_name": explicit_provider or provider_name or "",
            }

        # Sanitize and build final result
        _pname = _extract_str(
            caps.get("provider_name") or caps.get("provider") or caps.get("name")
        )
        _model = _extract_str(caps.get("model") or caps.get("default_model"))
        _pf = caps.get("provider_family") if _valid_str(caps.get("provider_family") or "") else None
        if not _pf and _pname:
            _pf = _map_provider_family_impl(_pname)
        _pf = _pf or "default"

        provider_capabilities = {
            "supports_native_tools": bool(caps.get("supports_native_tools", False)),
            "provider_family": _pf,
            "model": explicit_model or _model,
            "provider_name": explicit_provider or _pname or "",
            "provider_supports_parallel_tools": bool(
                caps.get("provider_supports_parallel_tools", False)
            ),
            "supports_function_call": bool(caps.get("supports_function_call", False)),
            "supports_streaming": bool(caps.get("supports_streaming", False)),
        }
        if explicit_provider:
            provider_capabilities["provider_family"] = _map_provider_family_impl(
                provider_capabilities["provider_name"]
            )

        # Apply Tier 0 override: providers.json is authoritative for
        # supports_native_tools when an explicit provider was requested.
        # This corrects the case where _adapter is None (not yet registered in
        # ProviderManager) so all other tiers returned False.
        if _tier0_supports_native_tools is not None:
            provider_capabilities["supports_native_tools"] = _tier0_supports_native_tools

    except Exception:
        provider_capabilities = {}
    return provider_capabilities
