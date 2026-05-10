from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional, Sequence


def should_probe_provider(
    *, provider_config: Optional[dict], canonical_provider: Callable[[Any], str]
) -> bool:
    if not provider_config:
        return True

    is_local = bool(
        provider_config.get("base_url")
        or canonical_provider(provider_config.get("type") or "")
        in {"lm_studio", "ollama", "openai_compat", "local"}
    )
    if provider_config.get("active") is False and not is_local:
        return False
    return True


def determine_explicit_status(adapter: Any) -> Optional[str]:
    if not hasattr(adapter, "validate_connection"):
        return None
    validator = adapter.validate_connection
    if inspect.iscoroutinefunction(validator):
        return None
    try:
        valid = validator()
        if inspect.isawaitable(valid):
            close = getattr(valid, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            return None
        return "connected" if valid else "disconnected"
    except Exception:
        return None


async def validate_provider_connection(*, adapter: Any) -> bool:
    if not adapter:
        return False
    try:
        if hasattr(adapter, "validate_connection"):
            result = adapter.validate_connection()
            if inspect.isawaitable(result):
                return bool(await result)
            return bool(result)
        if hasattr(adapter, "get_models_from_api"):
            try:
                response = adapter.get_models_from_api()
                if inspect.isawaitable(response):
                    response = await response
                return response is not None
            except Exception:
                return False
        return True
    except Exception:
        return False


def probe_adapter_models(
    *,
    adapter: Any,
    provider_key: str,
    extract_models_from_api_response: Callable[[object], List[str]],
    normalize_lmstudio_models: Callable[[List[str]], List[str]],
) -> List[str]:
    if not hasattr(adapter, "get_models_from_api"):
        return []
    try:
        response = adapter.get_models_from_api()
    except Exception:
        response = None

    models = extract_models_from_api_response(response)
    if provider_key == "lm_studio":
        return normalize_lmstudio_models(models)
    return models


def cache_probed_models(
    *,
    provider_key: str,
    models: List[str],
    provider_models_cache: Dict[str, List[str]],
    module_models_cache: Dict[str, List[str]],
    module_models_cache_time: Dict[str, float],
    model_cache_lock: object,
    now: Callable[[], float],
) -> None:
    provider_models_cache[provider_key] = models
    with model_cache_lock:
        module_models_cache[provider_key] = models
        module_models_cache_time[provider_key] = now()


def publish_provider_probe_events(
    *,
    event_bus: Any,
    provider_key: str,
    models: Sequence[str],
    explicit_status: Optional[str],
) -> None:
    if not event_bus:
        return
    try:
        if models:
            event_bus.publish(
                "provider.models.list",
                {"provider": provider_key, "models": list(models)},
            )
            event_bus.publish(
                "provider.models.cached",
                {"provider": provider_key, "models": list(models)},
            )
            event_bus.publish(
                "provider.status.changed",
                {"provider": provider_key, "status": explicit_status or "connected"},
            )
        else:
            event_bus.publish("provider.models.empty", {"provider": provider_key})
            event_bus.publish(
                "provider.status.changed",
                {"provider": provider_key, "status": explicit_status or "disconnected"},
            )
    except Exception:
        pass


def publish_unknown_provider_status(
    *, event_bus: Any, provider_key: str, explicit_status: Optional[str]
) -> None:
    if not event_bus:
        return
    try:
        event_bus.publish(
            "provider.status.changed",
            {"provider": provider_key, "status": explicit_status or "unknown"},
        )
    except Exception:
        pass


def run_provider_probe_cycle(
    *,
    providers: Sequence[dict],
    providers_map: dict[str, Any],
    provider_models_cache: Dict[str, List[str]],
    module_models_cache: Dict[str, List[str]],
    module_models_cache_time: Dict[str, float],
    model_cache_lock: object,
    now: Callable[[], float],
    event_bus: Any,
    canonical_provider: Callable[[Any], str],
    should_probe_provider_fn: Callable[..., bool],
    determine_explicit_status_fn: Callable[[Any], Optional[str]],
    probe_adapter_models_fn: Callable[..., List[str]],
    cache_probed_models_fn: Callable[..., None],
    publish_provider_probe_events_fn: Callable[..., None],
    publish_unknown_provider_status_fn: Callable[..., None],
    logger: Any,
    get_loaded_context_length_fn: Callable[[Any, Optional[str]], Any],
    set_active_context_length_fn: Callable[[Any, str], None],
    is_active_provider_fn: Callable[[str, Optional[dict]], bool],
) -> None:
    for provider_key, adapter in list(providers_map.items()):
        try:
            provider_config = next(
                (
                    provider
                    for provider in providers
                    if canonical_provider(
                        provider.get("name") or provider.get("type") or ""
                    )
                    == provider_key
                ),
                None,
            )
            if not should_probe_provider_fn(
                provider_config=provider_config,
                canonical_provider=canonical_provider,
            ):
                continue

            if not adapter:
                if not provider_models_cache.get(provider_key):
                    provider_models_cache[provider_key] = []
                if event_bus:
                    try:
                        event_bus.publish(
                            "provider.status.changed",
                            {"provider": provider_key, "status": "disconnected"},
                        )
                    except Exception:
                        pass
                continue

            explicit_status = determine_explicit_status_fn(adapter)

            if hasattr(adapter, "get_models_from_api"):
                models = probe_adapter_models_fn(
                    adapter=adapter,
                    provider_key=provider_key,
                )

                if models:
                    cache_probed_models_fn(
                        provider_key=provider_key,
                        models=models,
                        provider_models_cache=provider_models_cache,
                        module_models_cache=module_models_cache,
                        module_models_cache_time=module_models_cache_time,
                        model_cache_lock=model_cache_lock,
                        now=now,
                    )
                    logger.info(
                        f"ProviderManager: cached models for {provider_key}: {models}"
                    )
                    publish_provider_probe_events_fn(
                        event_bus=event_bus,
                        provider_key=provider_key,
                        models=models,
                        explicit_status=explicit_status,
                    )
                    if event_bus:
                        try:
                            active_model = models[0] if models else ""
                            context_length = get_loaded_context_length_fn(
                                adapter, active_model
                            )
                            if context_length and context_length > 0:
                                if is_active_provider_fn(provider_key, provider_config):
                                    set_active_context_length_fn(context_length, provider_key)
                                    event_bus.publish(
                                        "provider.context_window",
                                        {
                                            "provider": provider_key,
                                            "model": active_model,
                                            "context_window": context_length,
                                        },
                                    )
                        except Exception:
                            pass
                else:
                    if not provider_models_cache.get(provider_key):
                        provider_models_cache[provider_key] = []
                    publish_provider_probe_events_fn(
                        event_bus=event_bus,
                        provider_key=provider_key,
                        models=[],
                        explicit_status=explicit_status,
                    )
            else:
                if not provider_models_cache.get(provider_key):
                    provider_models_cache[provider_key] = []
                publish_unknown_provider_status_fn(
                    event_bus=event_bus,
                    provider_key=provider_key,
                    explicit_status=explicit_status,
                )
        except Exception:
            try:
                provider_models_cache[provider_key] = []
            except Exception:
                pass
