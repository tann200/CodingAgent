from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.inference._protocols import LockProtocol
from src.core.messaging.event_types import ProviderModelsCached, ProviderModelsList


def load_provider_entries(raw: Any) -> List[dict]:
    providers = (
        raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    )
    return [provider for provider in providers if isinstance(provider, dict)]


def resolve_adapter_class(
    *,
    provider_type: str,
    camelize: Callable[[str], str],
) -> Tuple[Optional[type], Optional[str]]:
    module_name = f"src.core.inference.adapters.{provider_type}_adapter"
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        return (
            None,
            f'ProviderManager: adapter module import failed for type "{provider_type}": {exc}',
        )

    class_name = camelize(provider_type) + "Adapter"
    adapter_cls = getattr(module, class_name, None) or getattr(module, "Adapter", None)
    if adapter_cls is None:
        return None, f"ProviderManager: Adapter class not found in module {module_name}"
    return adapter_cls, None


def instantiate_adapter(
    *,
    adapter_cls: type,
    provider: dict,
    providers_config_path: Optional[str],
    normalize_models_for_provider: Callable[[dict], List[str]],
) -> Tuple[Optional[Any], Optional[str]]:
    try:
        if hasattr(adapter_cls, "from_provider_config"):
            try:
                return adapter_cls.from_provider_config(provider), None
            except TypeError:
                return adapter_cls.from_provider_config(**provider), None

        try:
            adapter = adapter_cls(
                name=provider.get("name"),
                config_path=str(providers_config_path)
                if providers_config_path
                else None,
                base_url=provider.get("base_url") or provider.get("url"),
                api_key=provider.get("api_key"),
                models=normalize_models_for_provider(provider),
            )
            return adapter, None
        except TypeError:
            try:
                return adapter_cls(provider), None
            except Exception:
                try:
                    return (
                        adapter_cls(provider.get("base_url") or provider.get("url")),
                        None,
                    )
                except Exception:
                    return adapter_cls(), None
    except Exception as exc:
        provider_name = provider.get("name") or provider.get("type") or "unknown"
        return (
            None,
            f"ProviderManager: failed to instantiate adapter for {provider_name}: {exc}",
        )


def attach_provider_metadata(adapter: Any, provider: dict) -> None:
    if adapter is None:
        return
    try:
        setattr(adapter, "provider", provider)
    except Exception:
        pass
    try:
        setattr(adapter, "missing_provider", False)
    except Exception:
        pass


def cache_static_provider_models(
    *,
    provider_key: str,
    provider: dict,
    normalize_models_for_provider: Callable[[dict], List[str]],
    provider_models_cache: Dict[str, List[str]],
    module_models_cache: Dict[str, List[str]],
    module_models_cache_time: Dict[str, float],
    model_cache_lock: LockProtocol,
    now: Callable[[], float],
    event_bus: Any,
) -> None:
    try:
        models = normalize_models_for_provider(provider)
        if not models:
            return

        provider_models_cache[provider_key] = models
        with model_cache_lock:
            module_models_cache[provider_key] = models
            module_models_cache_time[provider_key] = now()

        if event_bus:
            try:
                event_bus.publish_typed(ProviderModelsList(provider=provider_key, models=models))
                event_bus.publish_typed(ProviderModelsCached(provider=provider_key, models=models))
            except Exception:
                pass
    except Exception:
        pass


def load_registered_providers(
    *,
    providers: Sequence[dict],
    providers_map: Dict[str, Any],
    provider_models_cache: Dict[str, List[str]],
    module_models_cache: Dict[str, List[str]],
    module_models_cache_time: Dict[str, float],
    model_cache_lock: LockProtocol,
    now: Callable[[], float],
    event_bus: Any,
    providers_config_path: Optional[str],
    canonical_provider: Callable[[Optional[str]], str],
    resolve_adapter_class_fn: Callable[..., Tuple[Optional[type], Optional[str]]],
    instantiate_adapter_fn: Callable[..., Tuple[Optional[Any], Optional[str]]],
    attach_provider_metadata_fn: Callable[[Any, dict], None],
    cache_static_provider_models_fn: Callable[..., None],
    normalize_models_for_provider: Callable[[dict], List[str]],
    camelize: Callable[[str], str],
    logger: Any,
) -> None:
    for provider in providers:
        key = canonical_provider(provider.get("name") or provider.get("type") or "")

        provider_type = (
            str(provider.get("type") or "ollama").strip().lower().replace("-", "_")
        )
        adapter_cls, class_error = resolve_adapter_class_fn(
            provider_type=provider_type,
            camelize=camelize,
        )
        if class_error:
            logger.error(class_error)
            providers_map[key] = None
            continue

        adapter, adapter_error = instantiate_adapter_fn(
            adapter_cls=adapter_cls,
            provider=provider,
            providers_config_path=providers_config_path,
            normalize_models_for_provider=normalize_models_for_provider,
        )
        if adapter_error:
            logger.error(adapter_error)

        attach_provider_metadata_fn(adapter, provider)
        providers_map[key] = adapter

        cache_static_provider_models_fn(
            provider_key=key,
            provider=provider,
            normalize_models_for_provider=normalize_models_for_provider,
            provider_models_cache=provider_models_cache,
            module_models_cache=module_models_cache,
            module_models_cache_time=module_models_cache_time,
            model_cache_lock=model_cache_lock,
            now=now,
            event_bus=event_bus,
        )
