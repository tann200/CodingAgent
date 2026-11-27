from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

_MAX_STATIC_PROMPT_CACHE_ENTRIES = 64


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
    private_mode: bool = False,
) -> Tuple:
    try:
        serialized = json.dumps(tools, sort_keys=True, default=str)
        tools_key = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    except Exception:
        tools_key = "0"

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
        bool(private_mode),
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
    if len(cache) >= _MAX_STATIC_PROMPT_CACHE_ENTRIES and cache_key not in cache:
        try:
            oldest_key = next(iter(cache))
            cache.pop(oldest_key, None)
        except Exception:
            pass
    cache[cache_key] = value
