"""Provider family mapping and capability detection.

Extracted from orchestrator.py (Phase G3) — single responsibility.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


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


def _valid_str(x: Any) -> bool:
    # Prefer centralised helpers when available to keep heuristics consistent.
    try:
        from src.core.utils.strings import valid_str as _vs

        return _vs(x)
    except Exception:
        if not isinstance(x, str):
            return False
        s = x.strip()
        return bool(s) and "MagicMock" not in s


def _extract_str(candidate: Any) -> Optional[str]:
    """Extract a concrete string from various candidate types.

    Handles dicts (checking common keys) and plain strings. Returns None
    when no concrete string is found.
    """
    try:
        from src.core.utils.strings import extract_str as _es

        return _es(candidate)
    except Exception:
        if candidate is None:
            return None
        if isinstance(candidate, dict):
            for key in (
                "name",
                "id",
                "model",
                "provider_name",
                "type",
                "default_model",
            ):
                val = candidate.get(key)
                if _valid_str(val):
                    return str(val).strip()
            return None
        if _valid_str(candidate):
            return str(candidate).strip()
        return None


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
                    "model": model,
                    "provider_name": provider_name or "",
                }
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
        capabilities["provider_name"] = raw_name or raw_type or ""

        # ── 3. Active model ────────────────────────────────────────────
        active_model = getattr(orch._adapter, "default_model", None)
        capabilities["model"] = _extract_str(active_model)

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
