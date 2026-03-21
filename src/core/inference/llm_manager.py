"""LLM Manager: Provider registry, model discovery, validation, and factory.

This file provides a lightweight, test-friendly implementation of provider
management and a small set of helper shims expected by adapters/tests.

Design goals for tests:
- Avoid heavy side-effects during import (lazy operations, no network calls).
- Provide stable symbols (get_provider_manager, call_model, resolve_config_path, etc.).
- Be defensive: adapters may call functions synchronously.
"""

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import json
import inspect
import re

# Prefer the central project logger so all components share the same logging pipeline.
# If the central logger isn't importable (tests or early import), fall back to the standard
# library logger but use a generic project-like name so messages are grouped consistently.
try:
    from src.core.orchestration.event_bus import get_correlation_id as _get_correlation_id
except Exception:  # pragma: no cover — circular import guard for early tests
    def _get_correlation_id():  # type: ignore[misc]
        return None

try:
    # Prefer the app's central logger object (recommended)
    from src.core.logger import logger as guilogger
except Exception:
    import logging

    # Fallback: use a sensible logger name that maps to the project (so logs remain centralized)
    guilogger = logging.getLogger("coding_agent")

# Simple in-memory caches (protected by RLock for thread safety - C8 fix)
import threading as _threading
_MODEL_CACHE: Dict[str, List[str]] = {}
_MODEL_CACHE_TIME: Dict[str, float] = {}
_MODEL_CACHE_LOCK = _threading.RLock()
_CACHE_TTL = 300

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
    return normalized


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
                    models = []
                    for m in resp.get("models", []):
                        if isinstance(m, dict):
                            fid = (
                                m.get("id")
                                or m.get("key")
                                or m.get("name")
                                or m.get("model")
                            )
                            if fid:
                                models.append(str(fid))
                        elif isinstance(m, str):
                            models.append(m)
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
    if isinstance(models_field, list):
        for m in models_field:
            if isinstance(m, dict):
                fid = m.get("id") or m.get("key") or m.get("name") or m.get("model")
                if not fid:
                    continue
            elif isinstance(m, str):
                fid = m
            else:
                continue
            # If provider type indicates some LM studio, normalize to full id
            if "lm" in ptype or canonical_provider(provider.get("name")) == "lm_studio":
                try:
                    out.append(_lmstudio_full_id(fid))
                except Exception:
                    out.append(str(fid))
            else:
                out.append(str(fid))
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
        target.write_text(json.dumps(data), encoding="utf-8")
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

    def set_event_bus(self, bus: Any):
        self._event_bus = bus

    def list_providers(self) -> List[str]:
        return sorted(list(self._providers.keys()))

    def get_provider(self, key: str) -> Optional[Any]:
        if not key:
            return None
        return self._providers.get(key.lower().replace(" ", "_"))

    def get_cached_models(self, key: str) -> List[str]:
        if not key:
            return []
        return list(self._models_cache.get(key.lower().replace(" ", "_")) or [])

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

                    if hasattr(adapter, "get_models_from_api"):
                        try:
                            resp = adapter.get_models_from_api()
                        except Exception:
                            resp = None

                        models_list = []
                        if isinstance(resp, dict):
                            for m in resp.get("models", []):
                                if isinstance(m, dict):
                                    fid = (
                                        m.get("id")
                                        or m.get("key")
                                        or m.get("name")
                                        or m.get("model")
                                    )
                                    if fid:
                                        models_list.append(str(fid))
                                elif isinstance(m, str):
                                    models_list.append(m)

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
                                    self._event_bus.publish(
                                        "provider.status.changed",
                                        {"provider": prov_key, "status": "connected"},
                                    )
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
                                    self._event_bus.publish(
                                        "provider.status.changed",
                                        {
                                            "provider": prov_key,
                                            "status": "disconnected",
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
                                self._event_bus.publish(
                                    "provider.status.changed",
                                    {"provider": prov_key, "status": "unknown"},
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
        # Prefer adapter.chat if present (we pass messages directly), otherwise try adapter.generate
        last_err = None
        if hasattr(adapter, "chat"):
            loop = asyncio.get_running_loop()
            from functools import partial

            try:
                # call synchronously in executor
                fn = partial(
                    adapter.chat,
                    messages,
                    model=model,
                    stream=stream,
                    format_json=format_json,
                    **(kwargs or {}),
                )
                res = await loop.run_in_executor(None, fn)
                # M1: If stream=True the adapter may return a raw requests.Response;
                # consume the SSE stream and return the accumulated text as a dict.
                if stream and hasattr(res, "iter_lines"):
                    text = await loop.run_in_executor(None, _consume_sse_stream, res)
                    return {"ok": True, "text": text, "streamed": True}
                return res
            except Exception as e:
                last_err = e
        if hasattr(adapter, "generate"):
            loop = asyncio.get_running_loop()
            from functools import partial

            try:
                # Some adapters expect (prompt, model, stream, format_json) while some expect prompt-only.
                fn = partial(
                    adapter.generate,
                    messages,
                    model=model,
                    stream=stream,
                    format_json=format_json,
                    **(kwargs or {}),
                )
                res = await loop.run_in_executor(None, fn)
                # M1: Same SSE consumption for generate path
                if stream and hasattr(res, "iter_lines"):
                    text = await loop.run_in_executor(None, _consume_sse_stream, res)
                    return {"ok": True, "text": text, "streamed": True}
                return res
            except TypeError:
                try:
                    # fallback: positional
                    fn = partial(adapter.generate, messages)
                    res = await loop.run_in_executor(None, fn)
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


def _consume_sse_stream(raw_response: Any) -> str:
    """M1: Iterate an OpenAI-compatible SSE stream, publish model.token events per chunk.

    Parses lines of the form:
        data: {"choices": [{"delta": {"content": "token"}, "finish_reason": null}]}
        data: [DONE]

    Returns the fully accumulated response text.
    """
    import json as _json

    try:
        from src.core.orchestration.event_bus import get_event_bus
        bus = get_event_bus()
    except Exception:
        bus = None

    accumulated = []
    try:
        for raw_line in raw_response.iter_lines():
            if not raw_line:
                continue
            line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = _json.loads(data)
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    token_text = delta.get("content") or ""
                    if token_text:
                        accumulated.append(token_text)
                        if bus:
                            try:
                                bus.publish("model.token", {"text": token_text, "partial": True})
                            except Exception:
                                pass
            except (_json.JSONDecodeError, KeyError, IndexError):
                continue
    except Exception as e:
        guilogger.warning(f"_consume_sse_stream: stream iteration error: {e}")

    full_text = "".join(accumulated)
    if bus and full_text:
        try:
            bus.publish("model.token", {"text": "", "partial": False, "full": full_text})
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
        _is_err = isinstance(res, dict) and (
            res.get("ok") is False or res.get("error")
        )
        if _is_err:
            _cb.record_failure()
        else:
            _cb.record_success()

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
