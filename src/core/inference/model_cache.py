from __future__ import annotations

import time
from typing import Callable, List, MutableMapping, Optional


def get_cached_models_if_fresh(
    *,
    provider_key: str,
    cache: MutableMapping[str, List[str]],
    cache_time: MutableMapping[str, float],
    ttl: int,
    now: Optional[float] = None,
) -> Optional[List[str]]:
    current = time.time() if now is None else now
    if provider_key in cache and (current - cache_time.get(provider_key, 0)) < ttl:
        return cache[provider_key]
    return None


def store_cached_models(
    *,
    provider_key: str,
    models: List[str],
    cache: MutableMapping[str, List[str]],
    cache_time: MutableMapping[str, float],
    now: Optional[float] = None,
) -> None:
    current = time.time() if now is None else now
    cache[provider_key] = models
    cache_time[provider_key] = current


def extract_models_from_api_response(
    response: object,
    *,
    valid_str: Callable[[object], bool],
) -> List[str]:
    models: List[str] = []
    if not isinstance(response, dict):
        return models

    for model in response.get("models", []):
        if isinstance(model, dict):
            model_id = model.get("id") or model.get("key") or model.get("name") or model.get("model")
        elif isinstance(model, str):
            model_id = model
        else:
            model_id = None

        if model_id and valid_str(model_id):
            models.append(str(model_id).strip())
    return models
