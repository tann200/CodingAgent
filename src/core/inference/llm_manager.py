"""LLM Manager: Provider registry, model discovery, validation, and factory.

This file provides a lightweight, test-friendly implementation of provider
management and a small set of helper shims expected by adapters/tests.

Design goals for tests:
- Avoid heavy side-effects during import (lazy operations, no network calls).
- Provide stable symbols (get_provider_manager, call_model, resolve_config_path, etc.).
- Be defensive: adapters may call functions synchronously.
"""

import asyncio
import contextvars
import functools
import os
import time
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Awaitable,
    TypeVar,
)
from pathlib import Path
import json
import inspect

# Prefer the central project logger so all components share the same logging pipeline.
# If the central logger isn't importable (tests or early import), fall back to the standard
# library logger but use a generic project-like name so messages are grouped consistently.
try:
    from src.core.orchestration.event_bus import (
        get_correlation_id as _get_correlation_id,  # type: ignore[assignment]
    )
except Exception:  # pragma: no cover — circular import guard for early tests

    def _get_correlation_id() -> Optional[str]:  # type: ignore[misc]
        return None


try:
    # Prefer the app's central logger object (recommended)
    from src.core.logger import logger as guilogger
except Exception:
    import logging

    # Fallback: use a sensible logger name that maps to the project (so logs remain centralized)
    guilogger = logging.getLogger("coding_agent")

# CODE_QUALITY_AUDIT #7 fix: promote deferred get_token_budget_monitor import to
# module level so it isn't re-imported on every call_model() invocation.
# Still guarded in a try/except to handle environments where token_budget is absent.
try:
    from src.core.orchestration.token_budget import (
        get_token_budget_monitor as _get_token_budget_monitor,
    )
except Exception:
    _get_token_budget_monitor = None  # type: ignore[assignment]

try:
    # Helper to propagate ContextVars into worker threads
    from src.core.orchestration.event_bus import run_with_correlation
except Exception:
    # Fallback: simple shim that calls loop.run_in_executor when event_bus isn't importable
    T = TypeVar("T")

    def run_with_correlation(
        loop: Any, executor: Any, fn: Callable[..., T], *args: Any
    ) -> Awaitable[T]:
        """Fallback run_with_correlation that safely copies the current Context
        and runs the callable inside that context in the executor. This avoids
        circular import with event_bus while still propagating ContextVars.

        The return type is Awaitable[Any] to make type-checkers happy when the
        caller awaits the result of run_in_executor.
        """
        try:
            ctx = contextvars.copy_context()

            def _worker() -> T:
                rv = fn(*args)
                if inspect.isawaitable(rv):
                    return asyncio.run(rv)  # type: ignore[return-value]
                return rv  # type: ignore[return-value]

            sel_executor = executor
            if sel_executor is None:
                # Lazily obtain the shared executor from event_bus to avoid a
                # hard import that can create circular imports at module import
                # time. Import locally.
                try:
                    from src.core.orchestration.event_bus import _get_shared_executor

                    sel_executor = _get_shared_executor()
                except Exception:
                    sel_executor = None

            fn_partial = functools.partial(ctx.run, _worker)
            return loop.run_in_executor(sel_executor, fn_partial)
        except Exception:
            # Last-resort fallback: direct run_in_executor without context copy
            try:
                return loop.run_in_executor(executor, fn, *args)
            except Exception:
                # If even that fails, raise so callers see the problem instead of
                # silently returning an unresolved awaitable.
                raise


# Simple in-memory caches (protected by RLock for thread safety - C8 fix)
import threading as _threading

from src.core.inference.model_cache import (
    extract_models_from_api_response as _extract_models_from_api_response,
    get_cached_models_if_fresh as _get_cached_models_if_fresh,
    store_cached_models as _store_cached_models,
)
from src.core.inference.model_selection import (
    canonical_provider as _select_canonical_provider,
    load_provider as _load_provider_helper,
    lmstudio_full_id as _normalize_lmstudio_full_id,
    normalize_models_for_provider as _normalize_models_for_provider_helper,
    resolve_requested_model as _resolve_requested_model_helper,
    resolve_config_path as _resolve_config_path_helper,
    save_provider as _save_provider_helper,
    set_provider_active as _set_provider_active_helper,
    select_model_name as _select_model_name,
)
from src.core.inference.call_postprocess import (
    attempt_model_fallback as _attempt_model_fallback,
    publish_llm_response_hook as _publish_llm_response_hook,
    record_token_usage as _record_token_usage,
    update_circuit_breaker_for_result as _update_circuit_breaker_for_result,
)
from src.core.inference.runtime_call import (
    call_adapter_with_fallbacks as _call_adapter_with_fallbacks,
    instantiate_runtime_adapter as _instantiate_runtime_adapter,
    prepare_call_extra_args as _prepare_call_extra_args,
    select_runtime_provider_config as _select_runtime_provider_config,
)
from src.core.inference.streaming import (
    decode_sse_line as _decode_sse_line,
    extract_stream_deltas as _extract_stream_deltas,
    finalize_stream as _finalize_stream,
    parse_sse_chunk as _parse_sse_chunk,
    publish_stream_chunk as _publish_stream_chunk,
    split_thinking_content as _split_thinking_content,
)
from src.core.inference.provider_discovery import (
    get_active_models as _get_active_models_helper,
    get_models_for_provider_key as _get_models_for_provider_key_helper,
    get_models_from_provider_adapter as _get_models_from_provider_adapter,
    get_models_from_provider_cache as _get_models_from_provider_cache,
    get_models_from_provider_config as _get_models_from_provider_config,
)
from src.core.inference.provider_loading import (
    attach_provider_metadata as _attach_provider_metadata,
    cache_static_provider_models as _cache_static_provider_models,
    instantiate_adapter as _instantiate_adapter,
    load_registered_providers as _load_registered_providers,
    load_provider_entries as _load_provider_entries,
    resolve_adapter_class as _resolve_adapter_class,
)
from src.core.inference.provider_probe import (
    cache_probed_models as _cache_probed_models,
    determine_explicit_status as _determine_explicit_status,
    probe_adapter_models as _probe_adapter_models,
    publish_provider_probe_events as _publish_provider_probe_events,
    publish_unknown_provider_status as _publish_unknown_provider_status,
    run_provider_probe_cycle as _run_provider_probe_cycle,
    should_probe_provider as _should_probe_provider,
    validate_provider_connection as _validate_provider_connection,
)
from src.core.inference.provider_config import (
    get_active_provider_name as _get_active_provider_name_helper,
    canonical_provider_name as _canonical_provider_name,
    normalize_provider_models as _normalize_provider_models,
    resolve_providers_config_path as _resolve_providers_config_path,
    set_provider_active_flag as _set_provider_active_flag,
)
from src.core.utils.strings import valid_str as _valid_str


def _set_active_context_length_lazy(context_length: int, provider_key: str = "") -> None:
    """Lazy wrapper for provider_context.set_active_context_length to avoid circular imports."""
    import importlib
    mod = importlib.import_module("src.core.inference.provider_context")
    mod.set_active_context_length(context_length, provider_id=provider_key)


# Gap 3: Plugin hooks — lazy import so the registry is not required at import time.
try:
    from src.core.plugin.hook_registry import (
        registry as _hook_registry,
        HOOK_LLM_RESPONSE as _HOOK_LLM_RESPONSE,
    )

    _LLM_MGR_HAS_HOOKS = True
except Exception:
    _hook_registry = None  # type: ignore[assignment]
    _HOOK_LLM_RESPONSE = "llm.response"
    _LLM_MGR_HAS_HOOKS = False

_MODEL_CACHE: Dict[str, List[str]] = {}
_MODEL_CACHE_TIME: Dict[str, float] = {}
_MODEL_CACHE_LOCK = _threading.RLock()
_CACHE_TTL = 300

# Shared lock for all atomic providers.json read-modify-write operations.
# Imported by settings_panel.py so both modules share the same lock object.
_providers_json_lock = _threading.Lock()

# --- Helper functions ---


def canonical_provider(name: Optional[str]) -> str:
    """Return a strict canonical provider key.

    Only well-known LM Studio variants map to 'lm_studio'. Avoid substring matches
    so other provider names containing 'lm' are not misclassified.
    """
    return _select_canonical_provider(
        name,
        canonical_provider_name_fn=_canonical_provider_name,
    )


def _set_provider_active(provider_type: str, active: bool) -> None:
    """Atomically set the active flag for a provider entry in providers.json.

    Thread-safe via _providers_json_lock (shared with settings_panel).
    Used after OAuth login/logout to enable or disable a provider without
    requiring a full settings save cycle.
    """
    _set_provider_active_helper(
        provider_type=provider_type,
        active=active,
        set_provider_active_flag_fn=_set_provider_active_flag,
        resolve_config_path_fn=resolve_config_path,
        canonical_provider_fn=canonical_provider,
        lock=_providers_json_lock,
        logger=guilogger,
    )


def _get_models_for_provider_key(provider_key: str) -> List[str]:
    """Centralized model discovery for a provider key.

    Order of sources:
    - in-memory module cache (_MODEL_CACHE)
    - ProviderManager._models_cache
    - Adapter probe via get_models_from_api()
    - providers.json static listing (normalized via normalize_models_for_provider)
    Returns an empty list when none found.
    """
    return _get_models_for_provider_key_helper(
        provider_key=provider_key,
        manager=_provider_manager,
        cache=_MODEL_CACHE,
        cache_time=_MODEL_CACHE_TIME,
        cache_lock=_MODEL_CACHE_LOCK,
        cache_ttl=_CACHE_TTL,
        now=time.time,
        get_cached_models_if_fresh=_get_cached_models_if_fresh,
        store_cached_models=_store_cached_models,
        get_models_from_provider_cache_fn=_get_models_from_provider_cache,
        get_models_from_provider_adapter_fn=_get_models_from_provider_adapter,
        get_models_from_provider_config_fn=_get_models_from_provider_config,
        extract_models_from_api_response=_extract_models_from_api_response,
        normalize_lmstudio_models=lambda items: [_lmstudio_full_id(x) for x in items],
        load_provider=load_provider,
        normalize_models_for_provider=normalize_models_for_provider,
        valid_str=_valid_str,
    )


def normalize_models_for_provider(provider: Dict[str, Any]) -> List[str]:
    """Return a normalized list of model identifiers for a provider dict.

    Ensures LM Studio model ids are converted to full ids and returns a list of
    strings suitable for caching and selection.
    """
    return _normalize_models_for_provider_helper(
        provider,
        normalize_provider_models_fn=_normalize_provider_models,
        valid_str_fn=_valid_str,
        canonical_provider_fn=canonical_provider,
        lmstudio_full_id_fn=_lmstudio_full_id,
    )


def resolve_config_path(path: Optional[str] = None) -> Path:
    """Return path to providers.json. Prefer explicit path, otherwise src/config/providers.json."""
    return _resolve_config_path_helper(
        path,
        resolve_providers_config_path_fn=_resolve_providers_config_path,
        current_file=__file__,
    )


def select_model_name(models: List[Any], requested: Optional[str]) -> Optional[str]:
    return _select_model_name(models, requested)


def _lmstudio_full_id(raw: str) -> str:
    """Compatibility wrapper for LM Studio full-id normalization."""
    return _normalize_lmstudio_full_id(raw)


# Compatibility shims expected by adapters/tests
DEFAULT_TIMEOUT = 5
LM_DEFAULT_TIMEOUT = DEFAULT_TIMEOUT

# NOTE: proxy provider identifiers are now stored on ProviderManager so that
# other modules can extend or query the set via the manager API. See
# ProviderManager._PROXY_PROVIDER_IDENTIFIERS and add_proxy_identifier().


def call_requests(method: str, url: str, **kwargs) -> Any:
    """Light wrapper around requests methods to allow tests to monkeypatch requests.

    method: 'get'|'post' etc.
    """
    try:
        import requests
    except Exception:
        raise
    fn = getattr(requests, method.lower(), None)
    if fn is None:
        # fallback to requests.request
        return requests.request(
            method, url, timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT), **kwargs
        )
    if "timeout" not in kwargs:
        kwargs["timeout"] = DEFAULT_TIMEOUT
    return fn(url, **kwargs)


def post_stream_compatible(
    url: str,
    json_data: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
):
    """Post helper that prefers the simple signature used in tests/fakes.

    Avoids passing headers by default so test fakes without that kwarg don't fail.
    """
    try:
        import requests
    except Exception:
        raise
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    try:
        return requests.post(url, json=json_data, timeout=timeout)
    except TypeError:
        return requests.post(
            url, json=json_data, headers=(headers or {}), timeout=timeout
        )


def load_provider(path: Optional[str] = None) -> Any:
    """Load providers.json or single provider config.

    Accepts either an explicit path or uses resolve_config_path(None).
    Returns parsed JSON (dict or list) or None on error.
    """
    return _load_provider_helper(
        path,
        resolve_config_path_fn=resolve_config_path,
    )


def save_provider(
    data: Any, path: Optional[str] = None, initial_path: Optional[Path] = None
) -> bool:
    """Save provider config to disk. Accepts optional initial_path for compatibility."""
    return _save_provider_helper(
        data,
        path=path,
        initial_path=initial_path,
        resolve_config_path_fn=resolve_config_path,
        logger=guilogger,
    )


# Backwards compatible aliases expected by adapters
def lm_resolve_config_path(path: Optional[str] = None) -> Path:
    return resolve_config_path(path)


def lm_load_provider(path: Optional[str] = None) -> Any:
    return load_provider(path)


def lm_save_provider(
    data: Any, path: Optional[str] = None, initial_path: Optional[Path] = None
) -> bool:
    return save_provider(data, path=path, initial_path=initial_path)


def lm_select_model_name(
    models: List[Any], requested: Optional[str] = None
) -> Optional[str]:
    return select_model_name(models, requested)


def lm_call_requests(method: str, url: str, **kwargs) -> Any:
    return call_requests(method, url, **kwargs)


def lm_post_stream_compatible(
    url: str,
    json_data: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
):
    # delegate to post_stream_compatible but adapt signature
    return post_stream_compatible(
        url, json_data=json_data, headers=headers, timeout=timeout
    )


def _camelize(s: str) -> str:
    """Convert a snake_case or hyphenated string to CamelCase."""
    parts = [x for x in s.replace("_", " ").split() if x]
    return "".join(part.title() for part in parts)


# --- ProviderManager ---
class ProviderManager:
    def __init__(self, providers_config_path: Optional[str] = None):
        self._providers: Dict[str, Any] = {}
        self._initialized = False
        self._init_lock = _threading.Lock()
        self._models_cache: Dict[str, List[str]] = {}
        self._event_bus = None
        self.providers_config_path = providers_config_path
        # Conservative proxy identifiers — kept small. Callers should prefer
        # explicit adapter/provider flags (requires_functions, is_proxy) when
        # available. Use add_proxy_identifier() to add custom ids at runtime.
        self._PROXY_PROVIDER_IDENTIFIERS = frozenset(
            {
                "litellm",
                "lite_llm",
                "litellm_proxy",
                "ccr",
                "ccr_proxy",
            }
        )

    def add_proxy_identifier(self, identifier: str) -> None:
        """Add a new proxy identifier at runtime. The identifier is matched
        case-insensitively against adapter name/type strings. This method is
        intentionally conservative: it appends to the existing set without
        exposing direct mutation of the underlying frozenset.
        """
        try:
            # Convert to a set, mutate, and reassign as frozenset for safety.
            s = set(self._PROXY_PROVIDER_IDENTIFIERS)
            s.add(str(identifier).lower())
            self._PROXY_PROVIDER_IDENTIFIERS = frozenset(s)
        except Exception:
            pass

    def remove_proxy_identifier(self, identifier: str) -> None:
        """Remove a proxy identifier if present."""
        try:
            s = set(self._PROXY_PROVIDER_IDENTIFIERS)
            s.discard(str(identifier).lower())
            self._PROXY_PROVIDER_IDENTIFIERS = frozenset(s)
        except Exception:
            pass

    def set_event_bus(self, bus: Any):
        self._event_bus = bus

    def list_providers(self) -> List[str]:
        return sorted(list(self._providers.keys()))

    def get_provider(self, key: str) -> Optional[Any]:
        if not key:
            return None
        return self._providers.get(key.lower().replace(" ", "_"))

    def get_provider_capabilities(
        self, key_or_adapter: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Return a synthesized provider capabilities dict for a provider key or adapter.

        Args:
            key_or_adapter: Either a provider key string (name/type) or an adapter
                instance. When omitted, the currently active adapter is used.

        Returns a dict with at least the keys:
            - supports_native_tools (bool)
            - provider_family (str)
            - model (Optional[str])
            - provider_name (str)

        This helper centralises lightweight capability synthesis so callers (tests,
        context builders) can obtain a consistent view of provider properties
        without instantiating adapters themselves.
        """
        capabilities: Dict[str, Any] = {
            "supports_native_tools": False,
            "provider_family": "default",
            "model": None,
            "provider_name": "",
        }
        try:
            # Resolve adapter instance
            adapter = None
            if isinstance(key_or_adapter, str):
                adapter = self.get_provider(canonical_provider(key_or_adapter))
            elif key_or_adapter is None:
                adapter = self.get_active_adapter()
            else:
                adapter = key_or_adapter

            if not adapter:
                return capabilities

            def _extract_from_provider_attr(val: Any) -> tuple[str, str]:
                # returns (name, type)
                if isinstance(val, dict):
                    return (
                        str(val.get("name") or "") or "",
                        str(val.get("type") or "") or "",
                    )
                return ("", "")

            raw_name = ""
            raw_type = ""
            provider_attr = getattr(adapter, "provider", None)
            if isinstance(provider_attr, dict):
                raw_name, raw_type = _extract_from_provider_attr(provider_attr)
                capabilities["supports_native_tools"] = bool(
                    provider_attr.get("supports_native_tools", False)
                )

            # Fall back to adapter.name when provider dict didn't supply a concrete name
            if not _valid_str(raw_name):
                name_attr = getattr(adapter, "name", "")
                raw_name = str(name_attr or "") if isinstance(name_attr, str) else ""
            # Sanitize raw_type as well
            if not _valid_str(raw_type):
                raw_type = ""

            # Map provider family (simple, local mapping — intentionally small)
            lookup = (raw_type or raw_name).lower().replace("-", "_").replace(" ", "_")
            _MAP: Dict[str, str] = {
                "anthropic": "anthropic",
                "openai": "openai",
                "openrouter": "openai",
                "github_copilot": "openai",
                "copilot": "openai",
                "ollama": "local",
                "lm_studio": "local",
                "lmstudio": "local",
                "local": "local",
                "mock": "mock",
            }
            family = "default"
            if lookup in _MAP:
                family = _MAP[lookup]
            else:
                for k, v in _MAP.items():
                    if k in lookup:
                        family = v
                        break

            capabilities["provider_family"] = family
            # Only expose provider_name when it's a concrete string
            if _valid_str(raw_name):
                capabilities["provider_name"] = raw_name
            elif _valid_str(raw_type):
                capabilities["provider_name"] = raw_type

            # Active model (adapter may expose default_model) — only accept concrete strings
            active_model = getattr(adapter, "default_model", None)
            if _valid_str(active_model):
                capabilities["model"] = active_model

            # Copilot-like proxies: inspect model name to refine family
            if family == "openai" and capabilities.get("model"):
                m_lower = str(capabilities.get("model") or "").lower()
                if "claude" in m_lower:
                    capabilities["provider_family"] = "anthropic"
                elif "gemini" in m_lower:
                    capabilities["provider_family"] = "gemini"

            # Additional optional inferred flags
            capabilities["provider_supports_parallel_tools"] = bool(
                getattr(adapter, "supports_parallel_tools", False)
                or (
                    isinstance(provider_attr, dict)
                    and provider_attr.get("supports_parallel_tools", False)
                )
            )
            capabilities["supports_function_call"] = bool(
                getattr(adapter, "supports_function_call", False)
                or (
                    isinstance(provider_attr, dict)
                    and provider_attr.get("supports_function_call", False)
                )
            )
            capabilities["supports_streaming"] = bool(
                getattr(adapter, "supports_streaming", False)
            )

            # Context window: adapter may expose a numeric attribute or a getter
            ctx = None
            try:
                if hasattr(adapter, "context_window") and getattr(
                    adapter, "context_window"
                ):
                    ctx = int(getattr(adapter, "context_window") or 0)
                elif hasattr(adapter, "get_loaded_context_length"):
                    try:
                        # Some adapters accept a model name; prefer active_model when present
                        model_to_query = capabilities.get("model")
                        if model_to_query:
                            ctx = adapter.get_loaded_context_length(model_to_query)
                        else:
                            ctx = adapter.get_loaded_context_length(None)
                    except Exception:
                        # ignore errors from adapter probes
                        ctx = None
            except Exception:
                ctx = None
            if ctx:
                try:
                    capabilities["context_window"] = int(ctx)
                except Exception:
                    capabilities["context_window"] = ctx

        except Exception:
            # conservative fallback already prepared
            pass
        return capabilities

    def is_proxy_adapter(self, key_or_adapter: Optional[Any] = None) -> bool:
        """Conservative detection whether an adapter is a proxy that likely
        requires a non-empty tools/functions list (e.g., LiteLLM, CCR proxies).

        Accepts either a provider key string or an adapter instance. Returns
        True only when conservative heuristics indicate the adapter behaves
        like a proxy requiring functions/tools to be present.
        """
        try:
            adapter = None
            if isinstance(key_or_adapter, str):
                adapter = self.get_provider(canonical_provider(key_or_adapter))
            elif key_or_adapter is None:
                adapter = self.get_active_adapter()
            else:
                adapter = key_or_adapter
            if not adapter:
                return False

            # Explicit flags on adapter/provider config take precedence.
            prov_attr = getattr(adapter, "provider", None)
            if isinstance(prov_attr, dict) and prov_attr.get("requires_functions"):
                return True
            if getattr(adapter, "is_proxy", False) or getattr(
                adapter, "requires_functions", False
            ):
                return True

            # Fallback: conservative name-based detection using registered ids.
            adapter_name = str(getattr(adapter, "name", "") or "").lower()
            adapter_cls = getattr(adapter.__class__, "__name__", "").lower()
            prov_type = ""
            if isinstance(prov_attr, dict):
                prov_type = str(prov_attr.get("type") or "") or ""
            try:
                for pid in getattr(self, "_PROXY_PROVIDER_IDENTIFIERS", ()):  # type: ignore[attr-defined]
                    if (
                        pid in adapter_name
                        or pid in adapter_cls
                        or pid in prov_type.lower()
                        or canonical_provider(adapter_name) == pid
                        or canonical_provider(prov_type) == pid
                    ):
                        return True
            except Exception:
                # fall through to False
                pass
        except Exception:
            return False
        return False

    def get_cached_models(self, key: str) -> List[str]:
        if not key:
            return []
        return list(self._models_cache.get(key.lower().replace(" ", "_")) or [])

    def get_active_adapter(self) -> Optional[Any]:
        """Return the adapter instance for the currently active provider, or None.

        This is a convenience wrapper used by orchestration code/tests that
        previously expected ProviderManager to expose a simple accessor.
        """
        try:
            active = self.get_active_provider_name()
            if not active:
                return None
            return self.get_provider(active)
        except Exception:
            return None

    def get_active_models(self) -> List[str]:
        """Return the list of models for the currently active provider.

        Preference order:
        - cached models from ProviderManager._models_cache
        - adapter.models attribute
        - adapter.default_model as single-entry list
        - empty list when none found
        """
        return _get_active_models_helper(manager=self)

    def get_active_provider_name(self) -> Optional[str]:
        """Return the canonical key of the first provider marked active:true in providers.json.

        Returns None if no provider is explicitly active (so callers can fall back).
        Thread-safe: reads providers.json under _providers_json_lock.
        """
        return _get_active_provider_name_helper(
            providers_config_path=self.providers_config_path,
            resolve_config_path=resolve_config_path,
            canonical_provider=canonical_provider,
            lock=_providers_json_lock,
        )

    async def initialize(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            guilogger.info("ProviderManager.initialize: loading providers.json")
            cfg = resolve_config_path(self.providers_config_path)
            try:
                if not cfg.exists():
                    # No providers.json present; publish missing event and mark initialized
                    if self._event_bus:
                        try:
                            self._event_bus.publish(
                                "provider.config.missing", {"path": str(cfg)}
                            )
                        except Exception:
                            pass
                    self._initialized = True
                    return

                raw = json.loads(cfg.read_text(encoding="utf-8"))
                providers = _load_provider_entries(raw)
                _load_registered_providers(
                    providers=providers,
                    providers_map=self._providers,
                    provider_models_cache=self._models_cache,
                    module_models_cache=_MODEL_CACHE,
                    module_models_cache_time=_MODEL_CACHE_TIME,
                    model_cache_lock=_MODEL_CACHE_LOCK,
                    now=time.time,
                    event_bus=self._event_bus,
                    providers_config_path=self.providers_config_path,
                    canonical_provider=canonical_provider,
                    resolve_adapter_class_fn=_resolve_adapter_class,
                    instantiate_adapter_fn=_instantiate_adapter,
                    attach_provider_metadata_fn=_attach_provider_metadata,
                    cache_static_provider_models_fn=_cache_static_provider_models,
                    normalize_models_for_provider=normalize_models_for_provider,
                    camelize=_camelize,
                    logger=guilogger,
                )

                _run_provider_probe_cycle(
                    providers=providers,
                    providers_map=self._providers,
                    provider_models_cache=self._models_cache,
                    module_models_cache=_MODEL_CACHE,
                    module_models_cache_time=_MODEL_CACHE_TIME,
                    model_cache_lock=_MODEL_CACHE_LOCK,
                    now=time.time,
                    event_bus=self._event_bus,
                    canonical_provider=canonical_provider,
                    should_probe_provider_fn=_should_probe_provider,
                    determine_explicit_status_fn=_determine_explicit_status,
                    probe_adapter_models_fn=lambda **kwargs: _probe_adapter_models(
                        extract_models_from_api_response=lambda response: _extract_models_from_api_response(
                            response,
                            valid_str=_valid_str,
                        ),
                        normalize_lmstudio_models=lambda items: [
                            _lmstudio_full_id(x) for x in items
                        ],
                        **kwargs,
                    ),
                    cache_probed_models_fn=_cache_probed_models,
                    publish_provider_probe_events_fn=_publish_provider_probe_events,
                    publish_unknown_provider_status_fn=_publish_unknown_provider_status,
                    logger=guilogger,
                    get_loaded_context_length_fn=lambda adapter, active_model: adapter.get_loaded_context_length(
                        active_model
                    )
                    if hasattr(adapter, "get_loaded_context_length")
                    else None,
                    set_active_context_length_fn=lambda context_length, provider_key="": _set_active_context_length_lazy(context_length, provider_key),
                    is_active_provider_fn=lambda provider_key, provider_config: bool(
                        provider_config and provider_config.get("active")
                    ),
                )

            except Exception as e:
                guilogger.error(f"ProviderManager.initialize error: {e}")
            self._initialized = True

    async def validate_provider(self, name: str) -> bool:
        return await _validate_provider_connection(adapter=self.get_provider(name))


# Module-level singleton
_provider_manager: ProviderManager = ProviderManager()


def get_provider_manager() -> ProviderManager:
    return _provider_manager


_INIT_TASK: "asyncio.Task | None" = None  # held so GC cannot collect it (NEW-11)
_INIT_TASK_LOCK = _threading.Lock()


def _ensure_provider_manager_initialized_sync():
    global _INIT_TASK
    # BUG-FIX #1: check already initialized before spawning task
    if _provider_manager._initialized:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with _INIT_TASK_LOCK:
            if _provider_manager._initialized:
                return
            if _INIT_TASK is not None and not _INIT_TASK.done():
                return
            # Store the task so it is not garbage-collected before it completes.
            # Exceptions are logged via the done-callback.
            _INIT_TASK = asyncio.create_task(_provider_manager.initialize())

        def _log_init_exc(t: "asyncio.Task") -> None:
            if not t.cancelled() and t.exception():
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "ProviderManager async init failed: %s", t.exception()
                )

        def _clear_init_task(t: "asyncio.Task") -> None:
            global _INIT_TASK
            with _INIT_TASK_LOCK:
                if _INIT_TASK is t:
                    _INIT_TASK = None

        _INIT_TASK.add_done_callback(_log_init_exc)
        _INIT_TASK.add_done_callback(_clear_init_task)
    else:
        try:
            asyncio.run(_provider_manager.initialize())
        except Exception:
            pass


# --- Model discovery and wrappers ---
async def get_available_models(
    base_url: str, api_key: str, provider_name: str
) -> List[str]:
    if not provider_name:
        return []
    provider_key = canonical_provider(provider_name)
    if not provider_key:
        return []
    # Ensure provider manager initialized
    try:
        mgr = _provider_manager
        if not mgr._initialized:
            await mgr.initialize()
    except Exception:
        pass
    return _get_models_for_provider_key(provider_key)


async def get_structured_llm(
    provider_override: Optional[str] = None, model_override: Optional[str] = None
) -> Tuple[Any, Optional[str]]:
    mgr = _provider_manager
    if not mgr._initialized:
        await mgr.initialize()

    # Use module-level UserPrefs symbol so tests can monkeypatch src.core.llm_manager.UserPrefs.load
    try:
        prefs = UserPrefs.load()
    except Exception:
        # Create a dummy object for tests
        class DummyPrefs:
            selected_model_provider: Optional[str] = None
            selected_model_name: Optional[str] = None

        prefs = DummyPrefs()
    p_name = provider_override or getattr(prefs, "selected_model_provider", None)
    model_name = model_override or getattr(prefs, "selected_model_name", None)
    if not p_name:
        raise RuntimeError("No provider configured")
    p_key = canonical_provider(p_name)

    # Centralized model discovery (module cache, provider cache, adapter probe, providers.json)
    try:
        models = await get_available_models("", "", p_key)
    except Exception:
        models = []

    resolved = None
    resolved = _resolve_requested_model_helper(
        models,
        model_name,
        select_model_name_fn=select_model_name,
        event_bus=mgr._event_bus,
        provider_key=p_key,
    )

    adapter = mgr.get_provider(p_key)
    return adapter, resolved


async def _call_model_internal(
    messages: List[Dict[str, Any]],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    stream: bool = False,
    format_json: bool = False,
    tools: Optional[List[Any]] = None,
    **kwargs,
) -> Any:
    # Prefer ProviderManager-registered adapter
    try:
        mgr = _provider_manager
        # ensure provider manager initialized so mgr.get_provider works reliably
        if not mgr._initialized:
            await mgr.initialize()
        adapter = None
        if provider:
            adapter = mgr.get_provider(canonical_provider(provider))
        if adapter is None:
            raw = load_provider(None)
            selected = _select_runtime_provider_config(raw=raw, provider=provider)
            adapter = _instantiate_runtime_adapter(
                provider_config=selected,
                providers_config_path=getattr(mgr, "providers_config_path", None),
                resolve_adapter_class=_resolve_adapter_class,
                instantiate_adapter=_instantiate_adapter,
                normalize_models_for_provider=normalize_models_for_provider,
                camelize=_camelize,
            )
        if adapter is None:
            return {"ok": False, "error": "no_adapter_found"}
        call_extra_args = _prepare_call_extra_args(
            kwargs=dict(kwargs or {}),
            tools=tools,
            is_proxy_adapter=mgr.is_proxy_adapter,
            adapter=adapter,
            messages=messages,
        )
        return await _call_adapter_with_fallbacks(
            adapter=adapter,
            messages=messages,
            model=model,
            stream=stream,
            format_json=format_json,
            call_extra_args=call_extra_args,
            run_with_correlation=run_with_correlation,
            consume_sse_stream=_consume_sse_stream,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# #31: Circuit Breaker for LLM adapters
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """
    Simple three-state circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED).

    States:
        CLOSED   — requests pass through normally.
        OPEN     — requests are rejected immediately (fast-fail) after
                   *failure_threshold* consecutive failures.
        HALF_OPEN — after *recovery_timeout* seconds the breaker lets ONE probe
                   request through.  If it succeeds → CLOSED; if it fails → OPEN.

    Thread-safe: all state mutations are protected by an RLock.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = self.CLOSED
        self._failure_count = 0
        self._opened_at: float = 0.0
        self._lock = _threading.RLock()

    # -- public interface ---------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    def is_open(self) -> bool:
        """Return True when the breaker will reject the next call."""
        with self._lock:
            return self._current_state() == self.OPEN

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.time()

    # -- internal -----------------------------------------------------------

    def _current_state(self) -> str:
        if self._state == self.OPEN:
            if time.time() - self._opened_at >= self.recovery_timeout:
                self._state = self.HALF_OPEN
                return self.HALF_OPEN
        return self._state


# Per-provider circuit breaker registry (provider_key → CircuitBreaker)
_CIRCUIT_BREAKERS: Dict[str, "CircuitBreaker"] = {}
_CB_LOCK = _threading.RLock()

_CB_FAILURE_THRESHOLD = int(os.getenv("LLM_CB_FAILURE_THRESHOLD", "3"))
_CB_RECOVERY_TIMEOUT = float(os.getenv("LLM_CB_RECOVERY_TIMEOUT", "60"))


def get_circuit_breaker(provider_key: str) -> "CircuitBreaker":
    """Return (creating if necessary) the CircuitBreaker for *provider_key*."""
    with _CB_LOCK:
        if provider_key not in _CIRCUIT_BREAKERS:
            _CIRCUIT_BREAKERS[provider_key] = CircuitBreaker(
                failure_threshold=_CB_FAILURE_THRESHOLD,
                recovery_timeout=_CB_RECOVERY_TIMEOUT,
            )
        return _CIRCUIT_BREAKERS[provider_key]


def _consume_sse_stream(raw_response: Any, model: Optional[str] = None) -> str:
    """M1: Iterate an OpenAI-compatible SSE stream, publish model.token events per chunk.

    TUI-10: Also publishes ``response.stream_chunk`` events with an
    ``is_reasoning`` field so the TUI can route thinking tokens to the
    collapsible ``ThinkingProcess`` widget rather than the main stream view.

    Three reasoning-detection sources are tried in order:
      1. ``delta.reasoning_content`` / ``delta.thinking`` — structured field.
      2. ``<think>`` / ``</think>`` tag split inside ``delta.content``.
      3. ``delta.is_reasoning`` boolean flag (some providers).

    Parses lines of the form:
        data: {"choices": [{"delta": {"content": "token"}, "finish_reason": null}]}
        data: [DONE]

    Returns the fully accumulated response text.
    """
    # CODE_QUALITY_AUDIT #7 fix: json is already imported at module level.
    try:
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()
    except Exception:
        bus = None

    # TUI-10: check if model emits <think> tags so we can enable the tag-split path
    _model_id = model or ""
    try:
        from src.core.inference.thinking_utils import is_reasoning_model as _is_rm

        _tag_split_enabled = _is_rm(_model_id)
    except Exception:
        _tag_split_enabled = False

    # Track whether we are currently inside a <think> block
    _inside_think = False

    accumulated = []

    def _publish_chunk(chunk: str, is_reasoning: bool) -> None:
        _publish_stream_chunk(bus=bus, chunk=chunk, is_reasoning=is_reasoning)

    try:
        for raw_line in raw_response.iter_lines():
            data = _decode_sse_line(raw_line)
            if data is None:
                continue
            if data == "[DONE]":
                break
            chunk = _parse_sse_chunk(data)
            if chunk is None:
                continue
            extracted = _extract_stream_deltas(chunk)
            if extracted is None:
                continue

            delta, reasoning_delta, content_delta = extracted
            used_original_content = False

            if not reasoning_delta:
                (
                    split_reasoning,
                    content_delta,
                    _inside_think,
                    prepublished_text,
                    used_original_content,
                ) = _split_thinking_content(
                    content_delta=content_delta,
                    inside_think=_inside_think,
                    tag_split_enabled=_tag_split_enabled,
                    publish_chunk=_publish_chunk,
                )
                if prepublished_text:
                    accumulated.extend(prepublished_text)
                if split_reasoning:
                    reasoning_delta = split_reasoning

            if reasoning_delta:
                _publish_chunk(reasoning_delta, True)

            token_text = (
                content_delta
                if content_delta
                else (
                    delta.get("content") or ""
                    if not reasoning_delta and not used_original_content
                    else ""
                )
            )
            if token_text:
                accumulated.append(token_text)
                _publish_chunk(token_text, False)
    except Exception as e:
        guilogger.warning(f"_consume_sse_stream: stream iteration error: {e}")

    return _finalize_stream(bus=bus, accumulated=accumulated)


async def call_model(
    messages: List[Dict[str, Any]],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    stream: bool = False,
    format_json: bool = False,
    tools: Optional[List[Any]] = None,
    session_id: Optional[str] = None,
    **kwargs,
) -> Any:
    # Log correlation ID so LLM calls can be traced back to the originating agent turn (#26)
    _cid = _get_correlation_id()
    if _cid:
        guilogger.debug(f"call_model: cid={_cid} provider={provider!r} model={model!r}")

    # #31: Circuit-breaker fast-fail — skip call entirely when provider is known-bad
    _cb_key = canonical_provider(provider) if provider else ""
    if _cb_key:
        _cb = get_circuit_breaker(_cb_key)
        if _cb.is_open():
            guilogger.warning(
                f"call_model: circuit breaker OPEN for provider '{_cb_key}' — fast-failing"
            )
            return {"ok": False, "error": f"circuit_breaker_open:{_cb_key}"}

    res = await _call_model_internal(
        messages, provider, model, stream, format_json, tools, **kwargs
    )

    res = await _attempt_model_fallback(
        enabled=os.getenv("LLM_MANAGER_ENABLE_MODEL_FALLBACK") == "1",
        current_result=res,
        current_model=model,
        provider=provider,
        messages=messages,
        stream=stream,
        format_json=format_json,
        tools=tools,
        kwargs=kwargs,
        max_fallbacks=int(os.getenv("LLM_MANAGER_MAX_FALLBACKS", "2")),
        get_available_models=get_available_models,
        call_model_internal=_call_model_internal,
        on_success=(lambda: get_circuit_breaker(_cb_key).record_success())
        if _cb_key
        else None,
    )

    # #31: Record success/failure in the circuit breaker
    _update_circuit_breaker_for_result(
        provider_key=_cb_key,
        result=res,
        get_circuit_breaker=get_circuit_breaker,
    )

    # HR-6: Record actual token usage in the budget monitor so check_budget() has
    # real counts rather than rough character-length estimates.
    # FIX: generate() normalises usage to top-level keys (prompt_tokens,
    # completion_tokens, total_tokens) — NOT nested under "usage".  The old
    # res.get("usage") path always returned {} and this block never fired.
    _record_token_usage(
        session_id=session_id,
        result=res,
        get_token_budget_monitor=_get_token_budget_monitor,
    )

    # Gap 3: HOOK_LLM_RESPONSE — lets plugins observe every model response.
    _publish_llm_response_hook(
        enabled=_LLM_MGR_HAS_HOOKS,
        hook_registry=_hook_registry,
        hook_name=_HOOK_LLM_RESPONSE,
        result=res,
        model=model,
        provider=provider,
    )

    return res


# Attempt to expose UserPrefs at module level so tests can monkeypatch it easily
try:
    from src.core.user_prefs import UserPrefs  # type: ignore
except Exception:

    class UserPrefs:  # minimal fallback used only during import-time when real module is unavailable
        def __init__(
            self, data: Optional[Dict[str, Any]] = None, path: Optional[Path] = None
        ):
            self.data = data or {}
            self.path = Path(path) if path else None
            self.selected_model_provider = self.data.get("selected_model_provider")
            self.selected_model_name = self.data.get("selected_model_name")

        @classmethod
        def load(cls, path: Optional[str] = None):
            return cls()

        def save(self):
            return None


# Public exports
__all__ = [
    "ProviderManager",
    "get_provider_manager",
    "call_model",
    "get_available_models",
    "get_structured_llm",
    "canonical_provider",
    "resolve_config_path",
    "load_provider",
    "save_provider",
    "call_requests",
    "post_stream_compatible",
    "DEFAULT_TIMEOUT",
    "LM_DEFAULT_TIMEOUT",
]
