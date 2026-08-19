from __future__ import annotations

from typing import Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple


def _normalize_model_cache_value(value: object) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip().lower()
    except Exception:
        return ""


def compute_static_prompt_cache_key(
    *,
    role_name: str,
    active_skills: Sequence[str],
    tools: List[Dict],
    model_tier: Optional[str],
    provider_capabilities: Optional[Mapping[str, object]],
    model_name: str,
    use_native_tools: bool,
    is_simple_mode: bool,
    provider_variant: str,
    working_dir: str,
) -> Tuple:
    try:
        tools_key = hash(
            tuple(
                (tool.get("name", ""), (tool.get("description") or "")[:50])
                for tool in tools
            )
        )
    except Exception:
        tools_key = 0

    caps = provider_capabilities or {}
    provider_family = str(caps.get("provider_family", ""))
    provider_model = _normalize_model_cache_value(caps.get("model"))
    requested_model = _normalize_model_cache_value(model_name)
    return (
        role_name,
        tuple(active_skills),
        tools_key,
        model_tier or "",
        provider_family,
        provider_model,
        requested_model,
        use_native_tools,
        is_simple_mode,
        provider_variant,
        working_dir,
    )


def get_static_prompt_cache_entry(
    *,
    cache: Mapping[Tuple, str],
    cache_key: Tuple,
) -> Optional[str]:
    return cache.get(cache_key)


def store_static_prompt_cache_entry(
    *,
    cache: MutableMapping[Tuple, str],
    cache_key: Tuple,
    value: str,
) -> None:
    cache[cache_key] = value
