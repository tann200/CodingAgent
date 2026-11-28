"""LLM Manager: Provider registry, model discovery, validation, and factory.

This file provides a lightweight, test-friendly implementation of provider
management and a small set of helper shims expected by adapters/tests.

Design goals for tests:
- Avoid heavy side-effects during import (lazy operations, no network calls).
- Provide stable symbols (get_provider_manager, call_model, resolve_config_path, etc.).
- Be defensive: adapters may call functions synchronously.
"""

import asyncio
from contextlib import contextmanager
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


guilogger: Any = None
try:
    # Prefer the app's central logger object (recommended)
    from src.core.logger import logger as guilogger  # type: ignore[assignment]
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

    def run_with_correlation(  # type: ignore[misc]
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
                    # Handle asyncio.run() safely in worker threads
                    try:
                        loop = asyncio.get_running_loop()
                        # If we're in a thread with a running loop, create a new one
                        if loop.is_running():
                            new_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(new_loop)
                            try:
                                return new_loop.run_until_complete(rv)
                            finally:
                                new_loop.close()
                        else:
                            # Loop exists but not running - safe to use run_until_complete
                            return loop.run_until_complete(rv)
                    except RuntimeError:
                        # No running loop - safe to use asyncio.run
                        return asyncio.run(rv)  # type: ignore[return-value, arg-type]
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
import threading as _threading  # noqa: E402

from src.core.inference.model_cache import (  # noqa: E402
    extract_models_from_api_response as _extract_models_from_api_response,
    get_cached_models_if_fresh as _get_cached_models_if_fresh,
    store_cached_models as _store_cached_models,
)
from src.core.inference.model_selection import (  # noqa: E402
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
from src.core.inference.call_postprocess import (  # noqa: E402
    attempt_model_fallback as _attempt_model_fallback,
    publish_llm_response_hook as _publish_llm_response_hook,
    record_token_usage as _record_token_usage,
    update_circuit_breaker_for_result as _update_circuit_breaker_for_result,
)
from src.core.inference.provider_fallback import get_fallback_chain as _get_fallback_chain  # noqa: E402
from src.core.inference.runtime_call import (  # noqa: E402
    call_adapter_with_fallbacks as _call_adapter_with_fallbacks,
    instantiate_runtime_adapter as _instantiate_runtime_adapter,
    prepare_call_extra_args as _prepare_call_extra_args,
    select_runtime_provider_config as _select_runtime_provider_config,
)
from src.core.inference.provider_discovery import (  # noqa: E402
    get_models_for_provider_key as _get_models_for_provider_key_helper,
    get_models_from_provider_adapter as _get_models_from_provider_adapter,
    get_models_from_provider_cache as _get_models_from_provider_cache,
    get_models_from_provider_config as _get_models_from_provider_config,
)
from src.core.inference.provider_loading import (  # noqa: E402
    instantiate_adapter as _instantiate_adapter,
    load_provider_entries as _load_provider_entries,  # noqa: F401
    load_registered_providers as _load_registered_providers,  # noqa: F401
    resolve_adapter_class as _resolve_adapter_class,
)
from src.core.inference.provider_probe import (  # noqa: E402
    run_provider_probe_cycle as _run_provider_probe_cycle,  # noqa: F401
)
from src.core.inference.provider_config import (  # noqa: E402
    canonical_provider_name as _canonical_provider_name,
    normalize_provider_models as _normalize_provider_models,
    resolve_providers_config_path as _resolve_providers_config_path,
    set_provider_active_flag as _set_provider_active_flag,
)
from src.core.utils.strings import valid_str as _valid_str  # noqa: E402


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

from src.core.inference.circuit_breaker import (  # noqa: E402
    CircuitBreaker,  # noqa: F401
    _CB_FAILURE_THRESHOLD,  # noqa: F401
    _CB_LOCK,  # noqa: F401
    _CB_RECOVERY_TIMEOUT,  # noqa: F401
    _CIRCUIT_BREAKERS,  # noqa: F401
    get_circuit_breaker,  # noqa: F401
)
from src.core.inference.provider_manager import (  # noqa: E402
    ProviderManager,
    _CACHE_TTL,
    _MODEL_CACHE,
    _MODEL_CACHE_LOCK,
    _MODEL_CACHE_TIME,
    _providers_json_lock,
)
from src.core.inference.sse_stream_consumer import (  # noqa: E402
    consume_sse_stream as _consume_sse_stream_impl,
)
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
        cache_lock=_MODEL_CACHE_LOCK,  # type: ignore[arg-type]
        cache_ttl=_CACHE_TTL,
        now=time.time,
        get_cached_models_if_fresh=_get_cached_models_if_fresh,
        store_cached_models=_store_cached_models,
        get_models_from_provider_cache_fn=_get_models_from_provider_cache,
        get_models_from_provider_adapter_fn=_get_models_from_provider_adapter,
        get_models_from_provider_config_fn=_get_models_from_provider_config,
        extract_models_from_api_response=lambda r: _extract_models_from_api_response(r, valid_str=_valid_str),  # type: ignore[arg-type]
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




# Module-level singleton
_provider_manager: ProviderManager = ProviderManager()


def get_provider_manager() -> ProviderManager:
    return _provider_manager


def reload_provider_manager_sync() -> None:
    """Synchronously force reload of provider manager."""
    global _INIT_TASK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with _INIT_TASK_LOCK:
            _INIT_TASK = asyncio.create_task(_provider_manager.reload())
    else:
        try:
            asyncio.run(_provider_manager.reload())
        except Exception as e:
            guilogger.error(f"reload_provider_manager_sync error: {e}")


_INIT_TASK: "asyncio.Task | None" = None  # held so GC cannot collect it (NEW-11)
_INIT_TASK_LOCK = _threading.Lock()


def _ensure_provider_manager_initialized_sync(probe: bool = True):
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
            _init_coro = (
                _provider_manager.initialize()
                if probe
                else _provider_manager.initialize(probe=False)
            )
            _INIT_TASK = asyncio.create_task(_init_coro)

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
            _init_coro = (
                _provider_manager.initialize()
                if probe
                else _provider_manager.initialize(probe=False)
            )
            asyncio.run(_init_coro)
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
    prefs: Any = None
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


_ACTIVE_RUNTIME_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
    contextvars.ContextVar("_active_runtime_context", default=None)
)


@contextmanager
def active_runtime_context(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    adapter: Any = None,
):
    """Bind the orchestrator's active provider/model/adapter for nested calls."""
    token = _ACTIVE_RUNTIME_CONTEXT.set(
        {
            "provider": provider,
            "model": model,
            "adapter": adapter,
        }
    )
    try:
        yield
    finally:
        _ACTIVE_RUNTIME_CONTEXT.reset(token)


def get_active_runtime_context() -> Optional[Dict[str, Any]]:
    """Return the active orchestrator runtime provider/model context, if any."""
    return _ACTIVE_RUNTIME_CONTEXT.get(None)


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
        _runtime_ctx = _ACTIVE_RUNTIME_CONTEXT.get(None)
        if isinstance(_runtime_ctx, dict):
            if provider is None and isinstance(_runtime_ctx.get("provider"), str) and _runtime_ctx["provider"].strip():
                provider = _runtime_ctx["provider"].strip()
            if model is None and isinstance(_runtime_ctx.get("model"), str) and _runtime_ctx["model"].strip():
                model = _runtime_ctx["model"].strip()
        adapter = None
        if (
            isinstance(_runtime_ctx, dict)
            and _runtime_ctx.get("adapter") is not None
        ):
            _ctx_prov = _runtime_ctx.get("provider")
            if not provider or not _ctx_prov or canonical_provider(provider) == canonical_provider(_ctx_prov):
                adapter = _runtime_ctx["adapter"]
        if adapter is None and provider:
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
        # CRED-1: str(e) from adapter exceptions can include URLs with credentials.
        # Log only the exception type; return a generic error code to callers.
        guilogger.warning("_call_model_internal: %s", type(e).__name__)
        return {"ok": False, "error": "internal_error"}




def _consume_sse_stream(raw_response: Any, model: Optional[str] = None) -> str:
    """Thin wrapper delegating SSE stream consumption to ``sse_stream_consumer``."""
    return _consume_sse_stream_impl(raw_response, model=model, logger=guilogger)



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
    _runtime_ctx = _ACTIVE_RUNTIME_CONTEXT.get(None)
    if isinstance(_runtime_ctx, dict):
        if provider is None and isinstance(_runtime_ctx.get("provider"), str) and _runtime_ctx["provider"].strip():
            provider = _runtime_ctx["provider"].strip()
        if model is None and isinstance(_runtime_ctx.get("model"), str) and _runtime_ctx["model"].strip():
            model = _runtime_ctx["model"].strip()

    # Log correlation ID so LLM calls can be traced back to the originating agent turn (#26)
    _cid = _get_correlation_id()
    if _cid:
        guilogger.debug(f"call_model: cid={_cid} provider={provider!r} model={model!r}")

    # Fetch the global event bus once for this call so we can publish fallback
    # events without re-importing inside the hot path.
    try:
        from src.core.orchestration.event_bus import get_event_bus as _get_eb

        _call_event_bus = _get_eb()
    except Exception:
        _call_event_bus = None

    # #31: Circuit-breaker fast-fail — skip primary call when provider is known-bad
    _active_p = None
    try:
        _active_p = _provider_manager.get_active_provider_name()
    except Exception:
        pass
    _resolved_p = provider or _active_p or ""
    _cb_key = canonical_provider(_resolved_p) if _resolved_p else ""
    _cb_open = False
    if _cb_key:
        _cb = get_circuit_breaker(_cb_key)
        if not _cb.allow_request():
            guilogger.warning(
                f"call_model: circuit breaker OPEN for provider '{_cb_key}' — fast-failing primary call"
            )
            res = {"ok": False, "error": f"circuit_breaker_open:{_cb_key}"}
            _cb_open = True

    if not _cb_open:
        try:
            res = await _call_model_internal(
                messages, provider, model, stream, format_json, tools, **kwargs
            )
        except BaseException:
            if _cb_key:
                try:
                    get_circuit_breaker(_cb_key).release_probe()
                except Exception:
                    pass
            raise

        res, _cb_handled = await _attempt_model_fallback(
            enabled=os.getenv("LLM_MANAGER_ENABLE_MODEL_FALLBACK", "1") == "1",
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
        if not _cb_handled and _cb_key and isinstance(res, dict) and not res.get("ok"):
            # SPEC-14: Primary provider failed and model fallback did not recover.
            # Record primary failure before cross-provider fallback runs,
            # so a secondary provider's success doesn't reset primary's breaker!
            _update_circuit_breaker_for_result(
                provider_key=_cb_key,
                result=res,
                get_circuit_breaker=get_circuit_breaker,
            )
            _cb_handled = True
    else:
        _cb_handled = True

    # P1-1: Cross-provider fallback — if model-level fallback also failed, try
    # other registered providers before giving up entirely.
    res, _fallback_provider = await _get_fallback_chain().call(
        primary_result=res,
        primary_provider=provider,
        messages=messages,
        model=model,
        stream=stream,
        format_json=format_json,
        tools=tools,
        kwargs=kwargs,
        call_model_internal=_call_model_internal,
        get_provider_manager=get_provider_manager,
        get_circuit_breaker=get_circuit_breaker,
        publish=_call_event_bus.publish if _call_event_bus is not None else None,
    )

    # #31: Record success/failure in the circuit breaker.
    # G-01/G-02: skip when fallback already called on_success (record_success)
    # to prevent double-counting a successful recovery.
    if not _cb_handled:
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

    # Publish final token telemetry for streamed responses (whose adapter-level
    # @with_telemetry fired before stream consumption when counts were 0).
    if isinstance(res, dict) and res.get("streamed"):
        _raw_p: Any = res.get("prompt_tokens")
        _raw_c: Any = res.get("completion_tokens")
        _raw_lat: Any = res.get("latency")
        _sp = int(_raw_p or 0)
        _sc = int(_raw_c or 0)
        if _sp > 0 or _sc > 0:
            try:
                from src.core.inference.telemetry import publish_model_response
                from src.core.orchestration.event_bus import get_event_bus as _get_eb

                _tbus = _call_event_bus or _get_eb()
                if _tbus is not None:
                    publish_model_response(
                        _tbus,
                        str(res.get("provider") or provider or "unknown"),
                        str(res.get("model") or model or "unknown"),
                        _sp,
                        _sc,
                        _sp + _sc,
                        float(_raw_lat or 0.0),
                    )
            except Exception:
                pass

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

    class UserPrefs:  # type: ignore[no-redef]  # minimal fallback used only during import-time when real module is unavailable
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
