from __future__ import annotations

from typing import Any, Callable, List, Optional

from src.core.inference._protocols import LockProtocol, ProviderManagerProtocol


def get_models_from_provider_cache(
    *,
    manager: ProviderManagerProtocol,
    provider_key: str,
    normalize_lmstudio_models: Callable[[List[str]], List[str]],
) -> Optional[List[str]]:
    try:
        cached = manager.get_cached_models(provider_key)
        if cached:
            return normalize_lmstudio_models(cached) if provider_key == "lm_studio" else cached
    except Exception:
        pass
    return None


def get_models_from_provider_adapter(
    *,
    manager: ProviderManagerProtocol,
    provider_key: str,
    extract_models_from_api_response: Callable[[object], List[str]],
    normalize_lmstudio_models: Callable[[List[str]], List[str]],
) -> Optional[List[str]]:
    try:
        adapter = manager.get_provider(provider_key)
        if adapter and hasattr(adapter, "get_models_from_api"):
            try:
                response = adapter.get_models_from_api()
            except Exception:
                response = None
            models = extract_models_from_api_response(response)
            if models:
                return normalize_lmstudio_models(models) if provider_key == "lm_studio" else models
    except Exception:
        pass
    return None


def get_models_from_provider_config(
    *,
    manager: ProviderManagerProtocol,
    provider_key: str,
    load_provider: Callable[[Optional[str]], Any],
    normalize_models_for_provider: Callable[[dict], List[str]],
) -> Optional[List[str]]:
    try:
        raw = None
        if getattr(manager, "providers_config_path", None):
            raw = load_provider(manager.providers_config_path)
        if raw is None:
            raw = load_provider(None)
        providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        for provider in providers:
            key = (provider.get("name") or provider.get("type") or "").lower().replace(" ", "_")
            if key == provider_key:
                models = normalize_models_for_provider(provider)
                if models:
                    return models
    except Exception:
        pass
    return None


def get_active_models(*, manager: ProviderManagerProtocol) -> List[str]:
    try:
        active = manager.get_active_provider_name()
        if not active:
            return []
        models = manager.get_cached_models(active)
        if models:
            return models
        adapter = manager.get_provider(active)
        if not adapter:
            return []
        if hasattr(adapter, "models") and getattr(adapter, "models"):
            try:
                return list(getattr(adapter, "models"))
            except Exception:
                pass
        if hasattr(adapter, "default_model") and getattr(adapter, "default_model"):
            return [str(getattr(adapter, "default_model"))]
    except Exception:
        pass
    return []


def get_models_for_provider_key(
    *,
    provider_key: str,
    manager: ProviderManagerProtocol,
    cache: dict[str, List[str]],
    cache_time: dict[str, float],
    cache_lock: LockProtocol,
    cache_ttl: int,
    now: Callable[[], float],
    get_cached_models_if_fresh: Callable[..., Optional[List[str]]],
    store_cached_models: Callable[..., None],
    get_models_from_provider_cache_fn: Callable[..., Optional[List[str]]],
    get_models_from_provider_adapter_fn: Callable[..., Optional[List[str]]],
    get_models_from_provider_config_fn: Callable[..., Optional[List[str]]],
    extract_models_from_api_response: Callable[[object], List[str]],
    normalize_lmstudio_models: Callable[[List[str]], List[str]],
    load_provider: Callable[[Optional[str]], Any],
    normalize_models_for_provider: Callable[[dict], List[str]],
    valid_str: Callable[[object], bool],
) -> List[str]:
    try:
        current = now()
        with cache_lock:
            cached_models = get_cached_models_if_fresh(
                provider_key=provider_key,
                cache=cache,
                cache_time=cache_time,
                ttl=cache_ttl,
                now=current,
            )
            if cached_models is not None:
                return cached_models

        cached = get_models_from_provider_cache_fn(
            manager=manager,
            provider_key=provider_key,
            normalize_lmstudio_models=normalize_lmstudio_models,
        )
        if cached:
            return cached

        models = get_models_from_provider_adapter_fn(
            manager=manager,
            provider_key=provider_key,
            extract_models_from_api_response=lambda response: extract_models_from_api_response(
                response, valid_str=valid_str  # type: ignore[call-arg]
            ),
            normalize_lmstudio_models=normalize_lmstudio_models,
        )
        if models:
            with cache_lock:
                store_cached_models(
                    provider_key=provider_key,
                    models=models,
                    cache=cache,
                    cache_time=cache_time,
                )
            return models

        models = get_models_from_provider_config_fn(
            manager=manager,
            provider_key=provider_key,
            load_provider=load_provider,
            normalize_models_for_provider=normalize_models_for_provider,
        )
        if models:
            return models
    except Exception:
        pass
    return []

