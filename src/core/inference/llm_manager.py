"""LLM Manager: Provider registry, model discovery, validation, and factory.

This file provides a lightweight, test-friendly implementation of provider
management and a small set of helper shims expected by adapters/tests.

Design goals for tests:
- Avoid heavy side-effects during import (lazy operations, no network calls).
- Provide stable symbols (get_provider_manager, call_model, resolve_config_path, etc.).
- Be defensive: adapters may call functions synchronously.
"""

import asyncio
import functools
import os
import tempfile
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
import re

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
            import contextvars as _contextvars
            import functools as _functools
            import inspect as _inspect
            import asyncio as _asyncio

            ctx = _contextvars.copy_context()

            def _worker() -> T:
                rv = fn(*args)
                if _inspect.isawaitable(rv):
                    return _asyncio.run(rv)  # type: ignore[return-value]
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

            fn_partial = _functools.partial(ctx.run, _worker)
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
    if not name:
        return ""
    s = str(name).strip().lower()
    normalized = s.replace(" ", "_").replace("-", "_")
    lm_variants = {"lm", "lm_studio", "lmstudio", "lm_studio"}
    if normalized in lm_variants or normalized == "lmstudio":
        return "lm_studio"
    copilot_variants = {
        "copilot",
        "github_copilot",
        "github-copilot",
        "ghcopilot",
        "github copilot",
    }
    if normalized in copilot_variants:
        return "github_copilot"
    return normalized


def _set_provider_active(provider_type: str, active: bool) -> None:
    """Atomically set the active flag for a provider entry in providers.json.

    Thread-safe via _providers_json_lock (shared with settings_panel).
    Used after OAuth login/logout to enable or disable a provider without
    requiring a full settings save cycle.
    """
    cfg_path = resolve_config_path(None)
    target_key = canonical_provider(provider_type)
    with _providers_json_lock:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        providers = raw if isinstance(raw, list) else [raw]
        for p in providers:
            if canonical_provider(p.get("type") or p.get("name") or "") == target_key:
                p["active"] = active
                break
        new_text = json.dumps(providers, indent=2)
        fd, tmp = tempfile.mkstemp(dir=cfg_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp, cfg_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _get_models_for_provider_key(provider_key: str) -> List[str]:
    """Centralized model discovery for a provider key.

    Order of sources:
    - in-memory module cache (_MODEL_CACHE)
    - ProviderManager._models_cache
    - Adapter probe via get_models_from_api()
    - providers.json static listing (normalized via normalize_models_for_provider)
    Returns an empty list when none found.
    """
    out: List[str] = []
    try:
        # 1) module-level cache (RLock-protected)
        now = time.time()
        with _MODEL_CACHE_LOCK:
            if (
                provider_key in _MODEL_CACHE
                and (now - _MODEL_CACHE_TIME.get(provider_key, 0)) < _CACHE_TTL
            ):
                return _MODEL_CACHE[provider_key]

        mgr = _provider_manager
        # 2) ProviderManager cache
        try:
            cached = mgr.get_cached_models(provider_key)
            if cached:
                # ensure LM Studio models are full ids
                if provider_key == "lm_studio":
                    return [_lmstudio_full_id(m) for m in cached]
                return cached
        except Exception:
            pass

        # 3) Adapter probe
        try:
            adapter = mgr.get_provider(provider_key)
            if adapter and hasattr(adapter, "get_models_from_api"):
                try:
                    resp = adapter.get_models_from_api()
                except Exception:
                    resp = None
                if isinstance(resp, dict):
                    # Guarded sanitisation: accept only concrete strings and
                    # normalise LM Studio ids when appropriate. This prevents
                    # test double placeholders (MagicMock) from leaking into
                    # cached model lists.
                    try:
                        from src.core.utils.strings import valid_str as _vs

                        def _valid_str(x: Any) -> bool:
                            return _vs(x)
                    except Exception:

                        def _valid_str(x: Any) -> bool:
                            return (
                                isinstance(x, str)
                                and bool(x.strip())
                                and "MagicMock" not in x
                            )

                    models = []
                    for m in resp.get("models", []):
                        fid = None
                        if isinstance(m, dict):
                            fid = (
                                m.get("id")
                                or m.get("key")
                                or m.get("name")
                                or m.get("model")
                            )
                        elif isinstance(m, str):
                            fid = m
                        if fid and _valid_str(fid):
                            models.append(str(fid).strip())
                    if models:
                        if provider_key == "lm_studio":
                            models = [_lmstudio_full_id(x) for x in models]
                        with _MODEL_CACHE_LOCK:
                            _MODEL_CACHE[provider_key] = models
                            _MODEL_CACHE_TIME[provider_key] = time.time()
                        return models
        except Exception:
            pass

        # 4) fallback to providers.json static config
        try:
            raw = None
            if getattr(mgr, "providers_config_path", None):
                raw = load_provider(mgr.providers_config_path)
            if raw is None:
                raw = load_provider(None)
            providers = (
                raw
                if isinstance(raw, list)
                else ([raw] if isinstance(raw, dict) else [])
            )
            for p in providers:
                key = (p.get("name") or p.get("type") or "").lower().replace(" ", "_")
                if key == provider_key:
                    models = normalize_models_for_provider(p)
                    if models:
                        return models
        except Exception:
            pass
    except Exception:
        pass
    return out


def normalize_models_for_provider(provider: Dict[str, Any]) -> List[str]:
    """Return a normalized list of model identifiers for a provider dict.

    Ensures LM Studio model ids are converted to full ids and returns a list of
    strings suitable for caching and selection.
    """
    out: List[str] = []
    if not provider or not isinstance(provider, dict):
        return out
    ptype = str(provider.get("type") or "").lower()
    models_field = provider.get("models") or []

    # Guarded import for shared validator to avoid circular imports in tests.
    try:
        from src.core.utils.strings import valid_str as _vs

        def _valid_str(x: Any) -> bool:
            try:
                return bool(_vs(x))
            except Exception:
                return isinstance(x, str) and bool(x.strip()) and ("MagicMock" not in x)
    except Exception:

        def _valid_str(x: Any) -> bool:
            return isinstance(x, str) and bool(x.strip()) and ("MagicMock" not in x)

    if isinstance(models_field, list):
        for m in models_field:
            if isinstance(m, dict):
                fid = m.get("id") or m.get("key") or m.get("name") or m.get("model")
            elif isinstance(m, str):
                fid = m
            else:
                continue
            if not fid or not _valid_str(fid):
                continue
            fid_str = str(fid).strip()
            # If provider type indicates some LM studio, normalize to full id
            if "lm" in ptype or canonical_provider(provider.get("name")) == "lm_studio":
                try:
                    full = _lmstudio_full_id(fid_str)
                    if _valid_str(full):
                        out.append(full)
                except Exception:
                    out.append(fid_str)
            else:
                out.append(fid_str)
    return out


def resolve_config_path(path: Optional[str] = None) -> Path:
    """Return path to providers.json. Prefer explicit path, otherwise src/config/providers.json."""
    if path:
        return Path(path)
    return Path(__file__).parents[2] / "config" / "providers.json"


def select_model_name(models: List[Any], requested: Optional[str]) -> Optional[str]:
    if not models:
        return None
    names: List[str] = []
    for m in models:
        if isinstance(m, dict):
            fid = m.get("id") or m.get("key") or m.get("name")
            if fid:
                names.append(str(fid))
        elif isinstance(m, str):
            names.append(m)
    if requested:
        if requested in names:
            return requested
        for n in names:
            if n.endswith("/" + requested) or n.split("/")[-1] == requested:
                return n
        return None
    return names[0] if names else None


def _lmstudio_full_id(raw: str) -> str:
    """Return a canonical LM Studio full id for a model string.

    Heuristic:
    - If already contains '/', assume it's a full id and return unchanged.
    - If contains ':' like 'qwen3.5:9b', convert to 'vendor/name-suffix' where
      vendor is the alphabetic prefix of the model name (e.g., 'qwen').
    - Otherwise, return unchanged.
    """
    if not raw:
        return raw
    s = str(raw)
    if "/" in s:
        return s
    if ":" in s:
        left, right = s.split(":", 1)
        # vendor = leading alpha characters from left
        m = re.match(r"^([a-zA-Z]+)", left)
        vendor = m.group(1) if m else left
        return f"{vendor}/{left}-{right}"
    return s


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
    try:
        p = resolve_config_path(path)
        text = None
        try:
            # try direct read (Path.read_text) to respect monkeypatching of open in tests
            text = Path(p).read_text(encoding="utf-8")
        except Exception:
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except Exception:
                return None
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None
    except Exception:
        return None


def save_provider(
    data: Any, path: Optional[str] = None, initial_path: Optional[Path] = None
) -> bool:
    """Save provider config to disk. Accepts optional initial_path for compatibility."""
    try:
        target = None
        if initial_path:
            try:
                target = Path(initial_path)
            except Exception:
                target = None
        if target is None:
            target = resolve_config_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Preserve array format: if file already holds an array and data is a single dict,
        # wrap it so the file stays as [{...}] rather than reverting to {...}.
        to_write = data
        if isinstance(data, dict) and target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    # Replace provider with matching name, or append if new
                    name = data.get("name")
                    updated = [
                        p
                        if (not isinstance(p, dict) or p.get("name") != name)
                        else data
                        for p in existing
                    ]
                    if not any(
                        isinstance(p, dict) and p.get("name") == name for p in existing
                    ):
                        updated.append(data)
                    to_write = updated
            except Exception:
                pass
        target.write_text(json.dumps(to_write), encoding="utf-8")
        return True
    except Exception:
        return False


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


# --- ProviderManager ---
class ProviderManager:
    def __init__(self, providers_config_path: Optional[str] = None):
        self._providers: Dict[str, Any] = {}
        self._initialized = False
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

            # Helpers — be conservative and avoid consuming MagicMock placeholders
            def _valid_str(x: Any) -> bool:
                try:
                    from src.core.utils.strings import valid_str as _vs

                    return _vs(x)
                except Exception:
                    return (
                        isinstance(x, str) and bool(x.strip()) and "MagicMock" not in x
                    )

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
        try:
            active = self.get_active_provider_name()
            if not active:
                return []
            models = self.get_cached_models(active)
            if models:
                return models
            adapter = self.get_provider(active)
            if not adapter:
                return []
            if hasattr(adapter, "models") and getattr(adapter, "models"):
                try:
                    return list(getattr(adapter, "models"))
                except Exception:
                    pass
            if hasattr(adapter, "default_model") and getattr(adapter, "default_model"):
                return [str(getattr(adapter, "default_model"))]
            return []
        except Exception:
            return []

    def get_active_provider_name(self) -> Optional[str]:
        """Return the canonical key of the first provider marked active:true in providers.json.

        Returns None if no provider is explicitly active (so callers can fall back).
        Thread-safe: reads providers.json under _providers_json_lock.
        """
        try:
            cfg_path = resolve_config_path(self.providers_config_path)
            with _providers_json_lock:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            providers = (
                raw
                if isinstance(raw, list)
                else ([raw] if isinstance(raw, dict) else [])
            )
            for p in providers:
                if not isinstance(p, dict):
                    continue
                if p.get("active") is True:
                    key = canonical_provider(p.get("name") or p.get("type") or "")
                    if key:
                        return key
        except Exception:
            pass
        return None

    async def initialize(self):
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
            providers = (
                raw
                if isinstance(raw, list)
                else ([raw] if isinstance(raw, dict) else [])
            )
            for p in providers:
                if not isinstance(p, dict):
                    # Malformed provider entry is an error: surface to the user
                    guilogger.error(
                        f"ProviderManager.initialize: provider entry malformed, expected dict: {repr(p)[:200]}"
                    )
                    continue

                # canonical provider key
                key = canonical_provider(p.get("name") or p.get("type") or "")

                # Load adapter module using the provider type; expect adapters to follow naming convention
                ptype = str(p.get("type") or "ollama").strip().lower().replace("-", "_")
                module_name = f"src.core.inference.adapters.{ptype}_adapter"
                try:
                    import importlib

                    mod = importlib.import_module(module_name)
                except Exception as e:
                    guilogger.error(
                        f'ProviderManager: adapter module import failed for type "{ptype}": {e}'
                    )
                    self._providers[key] = None
                    continue

                # Resolve Adapter class by convention: CamelCase type + 'Adapter' or 'Adapter'
                def _camelize(s: str) -> str:
                    parts = [x for x in s.replace("_", " ").split() if x]
                    return "".join(part.title() for part in parts)

                class_name = _camelize(ptype) + "Adapter"
                AdapterCls = getattr(mod, class_name, None) or getattr(
                    mod, "Adapter", None
                )
                if AdapterCls is None:
                    guilogger.error(
                        f"ProviderManager: Adapter class not found in module {module_name}"
                    )
                    self._providers[key] = None
                    continue

                # Instantiate adapter in a simple, predictable way. Adapters may provide
                # a factory `from_provider_config(provider_dict)` but otherwise accept
                # structured named args or the provider dict as a last resort.
                adapter = None
                try:
                    if hasattr(AdapterCls, "from_provider_config"):
                        try:
                            adapter = AdapterCls.from_provider_config(p)
                        except TypeError:
                            adapter = AdapterCls.from_provider_config(**p)
                    else:
                        # First choice: prefer explicit named args adapters commonly support
                        try:
                            cfg_path = (
                                str(self.providers_config_path)
                                if self.providers_config_path
                                else None
                            )
                            adapter = AdapterCls(
                                name=p.get("name"),
                                config_path=cfg_path,
                                base_url=p.get("base_url") or p.get("url"),
                                api_key=p.get("api_key"),
                                models=normalize_models_for_provider(p),
                            )
                        except TypeError:
                            # Try passing provider dict or base_url as single arg
                            try:
                                adapter = AdapterCls(p)
                            except Exception:
                                try:
                                    adapter = AdapterCls(
                                        p.get("base_url") or p.get("url")
                                    )
                                except Exception:
                                    adapter = AdapterCls()
                except Exception as e:
                    guilogger.error(
                        f"ProviderManager: failed to instantiate adapter for {key}: {e}"
                    )
                    adapter = None

                # Attach provider metadata and cache static models if present
                if adapter is not None:
                    try:
                        setattr(adapter, "provider", p)
                    except Exception:
                        pass
                    try:
                        setattr(adapter, "missing_provider", False)
                    except Exception:
                        pass

                self._providers[key] = adapter

                # Cache models declared in providers.json using a centralized helper
                try:
                    models_list_static = normalize_models_for_provider(p)
                    if models_list_static:
                        self._models_cache[key] = models_list_static
                        with _MODEL_CACHE_LOCK:
                            _MODEL_CACHE[key] = models_list_static
                            _MODEL_CACHE_TIME[key] = time.time()
                        if self._event_bus:
                            try:
                                self._event_bus.publish(
                                    "provider.models.list",
                                    {"provider": key, "models": models_list_static},
                                )
                                self._event_bus.publish(
                                    "provider.models.cached",
                                    {"provider": key, "models": models_list_static},
                                )
                            except Exception:
                                pass
                except Exception:
                    pass

            # Probe adapters for models (adapters may be network-backed; tests can monkeypatch)
            for prov_key, adapter in list(self._providers.items()):
                try:
                    # Skip probe for explicitly inactive providers
                    prov_cfg = next(
                        (
                            p
                            for p in providers
                            if canonical_provider(p.get("name") or p.get("type") or "")
                            == prov_key
                        ),
                        None,
                    )
                    # Skip probe for explicitly inactive providers, unless they are
                    # local/self-hosted (base_url present) — those should always be
                    # probed so the TUI can show whether LM Studio / Ollama is running.
                    _is_local_prov = bool(
                        prov_cfg
                        and (
                            prov_cfg.get("base_url")
                            or canonical_provider(prov_cfg.get("type") or "")
                            in {"lm_studio", "ollama", "openai_compat", "local"}
                        )
                    )
                    if (
                        prov_cfg
                        and prov_cfg.get("active") is False
                        and not _is_local_prov
                    ):
                        continue

                    if not adapter:
                        if not self._models_cache.get(prov_key):
                            self._models_cache[prov_key] = []
                        if self._event_bus:
                            try:
                                self._event_bus.publish(
                                    "provider.status.changed",
                                    {"provider": prov_key, "status": "disconnected"},
                                )
                            except Exception:
                                pass
                        continue

                    # --- Determine connection status ---
                    # Priority: validate_connection() > get_models_from_api() result.
                    # validate_connection() is preferred for providers like GitHub Copilot
                    # that use OAuth tokens (no network call needed to check auth state).
                    explicit_status: Optional[str] = None
                    if hasattr(adapter, "validate_connection"):
                        try:
                            valid = adapter.validate_connection()
                            if inspect.isawaitable(valid):
                                # Sync context — skip awaitable; fall through to probe
                                pass
                            else:
                                explicit_status = (
                                    "connected" if valid else "disconnected"
                                )
                        except Exception:
                            pass

                    if hasattr(adapter, "get_models_from_api"):
                        try:
                            resp = adapter.get_models_from_api()
                        except Exception:
                            resp = None

                        models_list = []
                        # Validate and sanitise probe results: only accept
                        # concrete non-empty strings (filter MagicMock placeholders)
                        try:
                            from src.core.utils.strings import valid_str as _vs

                            def _valid_str(x: Any) -> bool:
                                try:
                                    return bool(_vs(x))
                                except Exception:
                                    return (
                                        isinstance(x, str)
                                        and bool(x.strip())
                                        and ("MagicMock" not in x)
                                    )
                        except Exception:

                            def _valid_str(x: Any) -> bool:
                                return (
                                    isinstance(x, str)
                                    and bool(x.strip())
                                    and ("MagicMock" not in x)
                                )

                        if isinstance(resp, dict):
                            for m in resp.get("models", []):
                                fid = None
                                if isinstance(m, dict):
                                    fid = (
                                        m.get("id")
                                        or m.get("key")
                                        or m.get("name")
                                        or m.get("model")
                                    )
                                elif isinstance(m, str):
                                    fid = m
                                if fid and _valid_str(fid):
                                    models_list.append(str(fid).strip())

                        # Normalize LM Studio ids if needed
                        if prov_key == "lm_studio":
                            models_list = [_lmstudio_full_id(x) for x in models_list]

                        if models_list:
                            self._models_cache[prov_key] = models_list
                            with _MODEL_CACHE_LOCK:
                                _MODEL_CACHE[prov_key] = models_list
                                _MODEL_CACHE_TIME[prov_key] = time.time()
                            guilogger.info(
                                f"ProviderManager: cached models for {prov_key}: {models_list}"
                            )
                            if self._event_bus:
                                try:
                                    self._event_bus.publish(
                                        "provider.models.list",
                                        {"provider": prov_key, "models": models_list},
                                    )
                                    self._event_bus.publish(
                                        "provider.models.cached",
                                        {"provider": prov_key, "models": models_list},
                                    )
                                    # validate_connection() overrides model-probe status
                                    final_status = explicit_status or "connected"
                                    self._event_bus.publish(
                                        "provider.status.changed",
                                        {"provider": prov_key, "status": final_status},
                                    )
                                    # Query context window for providers that support it
                                    # (e.g. LM Studio exposes /api/v0/models with
                                    # loaded_context_length per model).
                                    if hasattr(adapter, "get_loaded_context_length"):
                                        try:
                                            active_model = (
                                                models_list[0] if models_list else ""
                                            )
                                            ctx_len = adapter.get_loaded_context_length(
                                                active_model
                                            )
                                            if ctx_len and ctx_len > 0:
                                                # Set directly so headless mode (no TUI
                                                # event subscriber) also gets the live value.
                                                from src.core.inference.provider_context import (
                                                    set_active_context_length,
                                                )

                                                set_active_context_length(ctx_len)
                                                if self._event_bus:
                                                    self._event_bus.publish(
                                                        "provider.context_window",
                                                        {
                                                            "provider": prov_key,
                                                            "model": active_model,
                                                            "context_window": ctx_len,
                                                        },
                                                    )
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                        else:
                            # Don't overwrite static models cached from providers.json
                            if not self._models_cache.get(prov_key):
                                self._models_cache[prov_key] = []
                            if self._event_bus:
                                try:
                                    self._event_bus.publish(
                                        "provider.models.empty", {"provider": prov_key}
                                    )
                                    # validate_connection() overrides empty-models status.
                                    # This is the key fix for GitHub Copilot: when a token
                                    # is stored, status is "connected" even though the
                                    # static fallback models were returned.
                                    final_status = explicit_status or "disconnected"
                                    self._event_bus.publish(
                                        "provider.status.changed",
                                        {
                                            "provider": prov_key,
                                            "status": final_status,
                                        },
                                    )
                                except Exception:
                                    pass
                    else:
                        # Don't overwrite static models cached from providers.json
                        if not self._models_cache.get(prov_key):
                            self._models_cache[prov_key] = []
                        if self._event_bus:
                            try:
                                final_status = explicit_status or "unknown"
                                self._event_bus.publish(
                                    "provider.status.changed",
                                    {"provider": prov_key, "status": final_status},
                                )
                            except Exception:
                                pass
                except Exception:
                    try:
                        self._models_cache[prov_key] = []
                    except Exception:
                        pass
                    continue

        except Exception as e:
            guilogger.error(f"ProviderManager.initialize error: {e}")
        self._initialized = True

    async def validate_provider(self, name: str) -> bool:
        prov = self.get_provider(name)
        if not prov:
            return False
        try:
            if hasattr(prov, "validate_connection"):
                res = prov.validate_connection()
                if inspect.isawaitable(res):
                    return await res
                return bool(res)
            if hasattr(prov, "get_models_from_api"):
                try:
                    resp = prov.get_models_from_api()
                    return resp is not None
                except Exception:
                    return False
            return True
        except Exception:
            return False


# Module-level singleton
_provider_manager: ProviderManager = ProviderManager()


def get_provider_manager() -> ProviderManager:
    return _provider_manager


_INIT_TASK: "asyncio.Task | None" = None  # held so GC cannot collect it (NEW-11)


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
        # Store the task so it is not garbage-collected before it completes.
        # Exceptions are logged via the done-callback.
        _INIT_TASK = asyncio.create_task(_provider_manager.initialize())

        def _log_init_exc(t: "asyncio.Task") -> None:
            if not t.cancelled() and t.exception():
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "ProviderManager async init failed: %s", t.exception()
                )

        _INIT_TASK.add_done_callback(_log_init_exc)
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
    if models:
        try:
            # select_model_name handles matching both short and full ids
            sel = select_model_name(models, model_name)
            if sel:
                resolved = sel
            else:
                # publish missing model event so callers can react
                try:
                    if mgr._event_bus:
                        mgr._event_bus.publish(
                            "provider.model.missing",
                            {
                                "provider": p_key,
                                "requested": model_name,
                                "available": models,
                            },
                        )
                except Exception:
                    pass
                resolved = None
        except Exception:
            resolved = None

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
            # try loading from providers.json
            raw = load_provider(None)
            providers = (
                raw
                if isinstance(raw, list)
                else ([raw] if isinstance(raw, dict) else [])
            )
            selected = None
            if provider:
                for p in providers:
                    if (p.get("name") or "").lower() == str(provider).lower() or (
                        p.get("type") or ""
                    ).lower() == str(provider).lower():
                        selected = p
                        break
            if not selected and providers:
                selected = providers[0]
            if selected:
                # instantiate adapter similar to ProviderManager logic
                ptype = (
                    str(selected.get("type") or "").strip().lower().replace("-", "_")
                    or "ollama"
                )
                # Load adapter module and try sensible instantiation fallbacks.
                try:
                    import importlib

                    mod = importlib.import_module(
                        f"src.core.inference.adapters.{ptype}_adapter"
                    )
                    class_name = (
                        "".join(
                            part.title() for part in ptype.replace("-", "_").split("_")
                        )
                        + "Adapter"
                    )
                    AdapterCls = getattr(mod, class_name, None) or getattr(
                        mod, "Adapter", None
                    )
                    if AdapterCls is None:
                        # fallback: pick any class ending with Adapter
                        for attr in dir(mod):
                            if attr.lower().endswith("adapter"):
                                candidate = getattr(mod, attr)
                                if isinstance(candidate, type):
                                    AdapterCls = candidate
                                    break
                    if AdapterCls is None:
                        raise ImportError("Adapter class not found")

                    adapter = None
                    # Try factory-based construction first
                    try:
                        if hasattr(AdapterCls, "from_provider_config"):
                            try:
                                adapter = AdapterCls.from_provider_config(selected)
                            except TypeError:
                                adapter = AdapterCls.from_provider_config(**selected)
                    except Exception:
                        adapter = None

                    if adapter is None:
                        # Try passing structured args
                        cfg_path = None
                        try:
                            cfg_path = (
                                str(mgr.providers_config_path)
                                if mgr.providers_config_path
                                else (selected.get("config_path") or None)
                            )
                        except Exception:
                            cfg_path = None
                        try:
                            adapter = AdapterCls(
                                name=selected.get("name"),
                                config_path=cfg_path,
                                api_key=selected.get("api_key"),
                                models=normalize_models_for_provider(selected),
                            )
                        except TypeError:
                            try:
                                adapter = AdapterCls(
                                    name=selected.get("name"),
                                    base_url=selected.get("base_url")
                                    or selected.get("url"),
                                    api_key=selected.get("api_key"),
                                )
                            except TypeError:
                                try:
                                    adapter = AdapterCls(
                                        selected.get("base_url") or selected.get("url")
                                    )
                                except Exception:
                                    try:
                                        adapter = AdapterCls(selected)
                                    except Exception:
                                        # Last resort: call without args
                                        adapter = AdapterCls()
                except Exception:
                    adapter = None
        if adapter is None:
            return {"ok": False, "error": "no_adapter_found"}
        # Prepare call kwargs so we can inject a safe noop function schema for
        # proxy adapters (LiteLLM-like) that require a non-empty tools/functions
        # list when callers did not supply any. We keep the original kwargs
        # behaviour but ensure the adapter receives a sensible `tools` arg.
        call_extra_args = dict(kwargs or {})
        try:
            if tools is not None:
                call_extra_args["tools"] = tools
            else:
                # Decide conservatively whether to inject a noop. Known proxy
                # targets (LiteLLM) are detected by adapter name/type.
                inject_noop = False
                try:
                    # Use the ProviderManager helper so detection logic is
                    # centralized and easier to maintain / test.
                    inject_noop = mgr.is_proxy_adapter(adapter)
                except Exception:
                    inject_noop = False

                if inject_noop:
                    noop_schema = {
                        "type": "function",
                        "function": {
                            "name": "_noop",
                            "description": "No-op placeholder injected by LLM manager to satisfy proxy requirement",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                    call_extra_args["tools"] = [noop_schema]
        except Exception:
            # Never fail LLM calls due to injection logic
            pass

        # Initialize last_err so static analyzers and all code paths have a
        # well-defined variable to inspect after attempted adapter calls.
        last_err = None

        if hasattr(adapter, "chat"):
            loop = asyncio.get_running_loop()
            # CODE_QUALITY_AUDIT #7 fix: functools.partial is now a top-level import.
            try:
                # call synchronously in executor
                fn = functools.partial(
                    adapter.chat,
                    messages,
                    model=model,
                    stream=stream,
                    format_json=format_json,
                    **call_extra_args,
                )
                # Propagate ContextVars (correlation id) into worker thread
                res = await run_with_correlation(loop, None, fn)
                # M1: If stream=True the adapter may return a raw requests.Response;
                # consume the SSE stream and return the accumulated text as a dict.
                if stream and hasattr(res, "iter_lines"):
                    text = await run_with_correlation(
                        loop, None, functools.partial(_consume_sse_stream, res, model)
                    )
                    return {"ok": True, "text": text, "streamed": True}
                return res
            except Exception as e:
                last_err = e
        if hasattr(adapter, "generate"):
            loop = asyncio.get_running_loop()
            try:
                # Some adapters expect (prompt, model, stream, format_json) while some expect prompt-only.
                fn = functools.partial(
                    adapter.generate,
                    messages,
                    model=model,
                    stream=stream,
                    format_json=format_json,
                    **call_extra_args,
                )
                # Propagate ContextVars into worker thread
                res = await run_with_correlation(loop, None, fn)
                # M1: Same SSE consumption for generate path
                if stream and hasattr(res, "iter_lines"):
                    text = await run_with_correlation(
                        loop, None, functools.partial(_consume_sse_stream, res, model)
                    )
                    return {"ok": True, "text": text, "streamed": True}
                return res
            except TypeError:
                try:
                    # fallback: positional
                    fn = functools.partial(adapter.generate, messages)
                    res = await run_with_correlation(loop, None, fn)
                    return res
                except Exception as e:
                    last_err = e
            except Exception as e:
                last_err = e
        if last_err:
            return {"ok": False, "error": str(last_err)}
        return {"ok": False, "error": "adapter_missing_generate_or_chat"}
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
    try:
        for raw_line in raw_response.iter_lines():
            if not raw_line:
                continue
            line = (
                raw_line
                if isinstance(raw_line, str)
                else raw_line.decode("utf-8", errors="replace")
            )
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}

                    # Source 1: structured reasoning field
                    reasoning_delta = (
                        delta.get("reasoning_content") or delta.get("thinking") or ""
                    )
                    # Source 3: explicit is_reasoning flag
                    if not reasoning_delta and delta.get("is_reasoning"):
                        reasoning_delta = delta.get("content") or ""
                        content_delta = ""
                    else:
                        content_delta = (
                            delta.get("content") or ""
                            if not reasoning_delta
                            else (delta.get("content") or "")
                        )

                    # Source 2: <think> tag split — only for known reasoning models
                    if not reasoning_delta and _tag_split_enabled and content_delta:
                        if "<think>" in content_delta and not _inside_think:
                            before, _, rest = content_delta.partition("<think>")
                            _inside_think = True
                            if before and bus:
                                try:
                                    bus.publish(
                                        "response.stream_chunk",
                                        {"chunk": before, "is_reasoning": False},
                                    )
                                    bus.publish(
                                        "model.token", {"text": before, "partial": True}
                                    )
                                except Exception:
                                    pass
                                accumulated.append(before)
                            if "</think>" in rest:
                                think_part, _, after = rest.partition("</think>")
                                _inside_think = False
                                if think_part and bus:
                                    try:
                                        bus.publish(
                                            "response.stream_chunk",
                                            {"chunk": think_part, "is_reasoning": True},
                                        )
                                    except Exception:
                                        pass
                                content_delta = after
                            else:
                                if rest and bus:
                                    try:
                                        bus.publish(
                                            "response.stream_chunk",
                                            {"chunk": rest, "is_reasoning": True},
                                        )
                                    except Exception:
                                        pass
                                content_delta = ""
                        elif "</think>" in content_delta and _inside_think:
                            think_part, _, after = content_delta.partition("</think>")
                            _inside_think = False
                            if think_part and bus:
                                try:
                                    bus.publish(
                                        "response.stream_chunk",
                                        {"chunk": think_part, "is_reasoning": True},
                                    )
                                except Exception:
                                    pass
                            content_delta = after
                        elif _inside_think:
                            reasoning_delta = content_delta
                            content_delta = ""

                    # Publish reasoning delta
                    if reasoning_delta and bus:
                        try:
                            bus.publish(
                                "response.stream_chunk",
                                {"chunk": reasoning_delta, "is_reasoning": True},
                            )
                        except Exception:
                            pass

                    # Publish normal content delta
                    token_text = (
                        content_delta
                        if content_delta
                        else (delta.get("content") or "" if not reasoning_delta else "")
                    )
                    if token_text:
                        accumulated.append(token_text)
                        if bus:
                            try:
                                bus.publish(
                                    "response.stream_chunk",
                                    {"chunk": token_text, "is_reasoning": False},
                                )
                                bus.publish(
                                    "model.token", {"text": token_text, "partial": True}
                                )
                            except Exception:
                                pass
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
    except Exception as e:
        guilogger.warning(f"_consume_sse_stream: stream iteration error: {e}")

    full_text = "".join(accumulated)
    if bus and full_text:
        try:
            bus.publish(
                "model.token", {"text": "", "partial": False, "full": full_text}
            )
            bus.publish("response.stream_end", {"full_text": full_text})
        except Exception:
            pass
    return full_text


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

    if os.getenv("LLM_MANAGER_ENABLE_MODEL_FALLBACK") == "1":
        is_error = False
        if isinstance(res, dict):
            if (
                res.get("ok") is False
                or "error" in res
                or (
                    res.get("meta")
                    and isinstance(res.get("meta"), dict)
                    and res["meta"].get("error")
                )
            ):
                is_error = True

        if is_error:
            # Attempt fallback — limit attempts to avoid N×120s cascading timeouts (H5 fix)
            _max_fallbacks = int(os.getenv("LLM_MANAGER_MAX_FALLBACKS", "2"))
            try:
                models = await get_available_models("", "", provider or "")
                if models:
                    _attempts = 0
                    for m in models:
                        if m == model:
                            continue
                        if _attempts >= _max_fallbacks:
                            break
                        _attempts += 1
                        fb_res = await _call_model_internal(
                            messages,
                            provider,
                            m,
                            stream,
                            format_json,
                            tools,
                            **kwargs,
                        )
                        is_fb_err = False
                        if isinstance(fb_res, dict):
                            if (
                                fb_res.get("ok") is False
                                or "error" in fb_res
                                or (
                                    fb_res.get("meta")
                                    and isinstance(fb_res.get("meta"), dict)
                                    and fb_res["meta"].get("error")
                                )
                            ):
                                is_fb_err = True
                        if not is_fb_err:
                            if _cb_key:
                                get_circuit_breaker(_cb_key).record_success()
                            return fb_res
            except Exception:
                pass

    # #31: Record success/failure in the circuit breaker
    if _cb_key:
        _cb = get_circuit_breaker(_cb_key)
        _is_err = isinstance(res, dict) and (res.get("ok") is False or res.get("error"))
        if _is_err:
            _cb.record_failure()
        else:
            _cb.record_success()

    # HR-6: Record actual token usage in the budget monitor so check_budget() has
    # real counts rather than rough character-length estimates.
    # FIX: generate() normalises usage to top-level keys (prompt_tokens,
    # completion_tokens, total_tokens) — NOT nested under "usage".  The old
    # res.get("usage") path always returned {} and this block never fired.
    if session_id and isinstance(res, dict):
        _pt = int(res.get("prompt_tokens") or 0)
        _ct = int(res.get("completion_tokens") or 0)
        _tt = int(res.get("total_tokens") or (_pt + _ct))
        if _tt > 0:
            try:
                if _get_token_budget_monitor is not None:
                    _get_token_budget_monitor().record_usage(session_id, _pt, _ct, _tt)
            except Exception:
                pass  # never let budget tracking break LLM calls

    # Gap 3: HOOK_LLM_RESPONSE — lets plugins observe every model response.
    if _LLM_MGR_HAS_HOOKS and _hook_registry is not None:
        try:
            _text = res.get("text", "") if isinstance(res, dict) else ""
            _hook_registry.call(
                _HOOK_LLM_RESPONSE,
                {
                    "content": _text,
                    "model": model or "",
                    "provider": provider or "",
                    "ok": res.get("ok", True) if isinstance(res, dict) else True,
                },
            )
        except Exception:
            pass

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
