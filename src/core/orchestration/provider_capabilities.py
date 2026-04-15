"""Provider family mapping and capability detection.

Extracted from orchestrator.py (Phase G3) — single responsibility.
"""

from __future__ import annotations

from typing import Any, Dict


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
    try:
        if not orch._adapter:
            return capabilities

        # ── 1. Extract raw name strings ────────────────────────────────
        raw_name: str = ""
        raw_type: str = ""

        provider_attr = getattr(orch._adapter, "provider", None)
        if isinstance(provider_attr, dict):
            raw_name = str(provider_attr.get("name") or "")
            raw_type = str(provider_attr.get("type") or "")
            capabilities["supports_native_tools"] = bool(
                provider_attr.get("supports_native_tools", False)
            )

        # Fall back to adapter.name (present on all OpenAICompatibleAdapter
        # subclasses and MockAdapter)
        if not raw_name:
            raw_name = str(getattr(orch._adapter, "name", "") or "")

        # ── 2. Resolve provider_family ─────────────────────────────────
        # Prefer the explicit "type" field when non-empty; otherwise use name.
        lookup_str = raw_type if raw_type else raw_name
        family = _map_provider_family_impl(lookup_str)
        # If type didn't resolve, try name as well
        if family == "default" and raw_type and raw_name:
            family = _map_provider_family_impl(raw_name)

        capabilities["provider_family"] = family
        capabilities["provider_name"] = raw_name or raw_type

        # ── 3. Active model ────────────────────────────────────────────
        active_model = getattr(orch._adapter, "default_model", None)
        capabilities["model"] = active_model

        # ── 4. GitHub Copilot model-name override ──────────────────────
        # Copilot proxies multiple model families under one provider type.
        # When the active model name reveals a more specific family, promote
        # provider_family so ContextBuilder picks the right prompt partial:
        #   claude-*   → "anthropic"
        #   gemini-*   → "gemini"
        #   o1/o3/o4-* → family stays "openai" but is_reasoning_model() fires
        if family == "openai" and active_model:
            m_lower = str(active_model).lower()
            if "claude" in m_lower:
                capabilities["provider_family"] = "anthropic"
            elif "gemini" in m_lower:
                capabilities["provider_family"] = "gemini"

    except Exception:
        pass
    return capabilities
