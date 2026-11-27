"""ProviderManager: provider registry, discovery, routing, and persistence.

Extracted from ``src/core/inference/llm_manager.py`` (TASK-43).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading as _threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.inference._protocols import ProviderManagerProtocol
from src.core.inference.model_cache import (
    extract_models_from_api_response as _extract_models_from_api_response,
)
from src.core.inference.model_selection import (
    canonical_provider as _select_canonical_provider,
    lmstudio_full_id as _normalize_lmstudio_full_id,
    normalize_models_for_provider as _normalize_models_for_provider_helper,
    resolve_config_path as _resolve_config_path_helper,
)
from src.core.inference.provider_config import (
    apply_user_prefs_to_providers as _apply_user_prefs_to_providers,
    canonical_provider_name as _canonical_provider_name,
    get_active_provider_name as _get_active_provider_name_helper,
    is_bundled_providers_config as _is_bundled_providers_config,
    normalize_provider_models as _normalize_provider_models,
    resolve_providers_config_path as _resolve_providers_config_path,
)
from src.core.inference.provider_discovery import (
    get_active_models as _get_active_models_helper,
)
from src.core.inference.provider_loading import (
    attach_provider_metadata as _attach_provider_metadata,
    cache_static_provider_models as _cache_static_provider_models,
    instantiate_adapter as _instantiate_adapter,
    load_provider_entries as _load_provider_entries,
    load_registered_providers as _load_registered_providers,
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
from src.core.messaging.event_types import ModelRoutingComplete, ProviderConfigMissing
from src.core.utils.strings import valid_str as _valid_str

guilogger: Any = None
try:
    from src.core.logger import logger as guilogger  # type: ignore[assignment]
except Exception:
    import logging

    guilogger = logging.getLogger("coding_agent")

_MODEL_CACHE: Dict[str, List[str]] = {}
_MODEL_CACHE_TIME: Dict[str, float] = {}
_MODEL_CACHE_LOCK = _threading.RLock()
_CACHE_TTL = 300

# Shared lock for all atomic providers.json read-modify-write operations.
_providers_json_lock = _threading.RLock()


def _camelize(s: str) -> str:
    """Convert a snake_case or hyphenated string to CamelCase."""
    parts = [x for x in s.replace("_", " ").split() if x]
    return "".join(part.title() for part in parts)


def canonical_provider(name: Optional[str]) -> str:
    return _select_canonical_provider(
        name, canonical_provider_name_fn=_canonical_provider_name
    )


def _lmstudio_full_id(entry: Any) -> str:
    return _normalize_lmstudio_full_id(entry)


def _lm_attr(name: str, default: Any) -> Any:
    """Resolve *name* from ``llm_manager`` when loaded so monkeypatches apply."""
    lm = sys.modules.get("src.core.inference.llm_manager")
    if lm is not None and hasattr(lm, name):
        return getattr(lm, name)
    return default


def normalize_models_for_provider(provider: Dict[str, Any]) -> List[str]:
    return _normalize_models_for_provider_helper(
        provider,
        normalize_provider_models_fn=_normalize_provider_models,
        valid_str_fn=_valid_str,
        canonical_provider_fn=canonical_provider,
        lmstudio_full_id_fn=_lmstudio_full_id,
    )


def _set_active_context_length_lazy(context_length: int, provider_key: str = "") -> None:
    """Lazy wrapper for provider_context.set_active_context_length to avoid circular imports."""
    import importlib

    mod = importlib.import_module("src.core.inference.provider_context")
    mod.set_active_context_length(context_length, provider_id=provider_key)


def _resolve_config_path_dynamic(path: Optional[str] = None) -> Path:
    """Delegate through llm_manager.resolve_config_path when loaded so monkeypatches apply."""
    lm = sys.modules.get("src.core.inference.llm_manager")
    fn = getattr(lm, "resolve_config_path", None) if lm is not None else None
    if callable(fn):
        return fn(path)
    return _resolve_config_path_helper(
        path,
        resolve_providers_config_path_fn=_resolve_providers_config_path,
        current_file=__file__,
    )


resolve_config_path = _resolve_config_path_dynamic


def _load_user_prefs_dynamic() -> Any:
    """Delegate through llm_manager.UserPrefs.load when loaded so monkeypatches apply."""
    lm = sys.modules.get("src.core.inference.llm_manager")
    up_cls = getattr(lm, "UserPrefs", None) if lm is not None else None
    if up_cls is not None and hasattr(up_cls, "load"):
        return up_cls.load()
    from src.core.user_prefs import UserPrefs

    return UserPrefs.load()


class ProviderManager(ProviderManagerProtocol):
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
        # P3-3: subscribe to model.routing so live model switches from the TUI
        # propagate into the inference layer without requiring a restart.
        try:
            bus.subscribe("model.routing", self._on_model_routing)
        except Exception:
            pass

    def _on_model_routing(self, payload: Any) -> None:
        """Handle a ``model.routing`` event published by the TUI or orchestrator.

        For same-provider model switch:
            Updates ``default_model`` on the active adapter so the next inference
            call picks the new model without a restart.

        For cross-provider switch (``provider`` key in payload differs from current):
            1. Writes the new ``active`` flag to ``providers.json`` so
               ``get_active_provider_name()`` reflects the change persistently.
            2. Re-instantiates the target adapter from its config so in-process
               calls immediately route to the new provider — no restart required.
            3. Updates ``default_model`` on the newly active adapter.
            4. Publishes ``model.routing.complete`` with the outcome.

        Args:
            payload: Dict with keys ``"selected"`` (model name) and optionally
                     ``"provider"`` (provider key/name).
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)
        try:
            if not isinstance(payload, dict):
                return
            selected_model: Optional[str] = payload.get("selected") or payload.get("model")
            provider_key: Optional[str] = payload.get("provider")
            if not selected_model:
                return

            switched_provider: Optional[str] = None

            # --- Cross-provider switch ----------------------------------------
            if provider_key:
                pkey = canonical_provider(provider_key)
                current = self.get_active_provider_name()

                if pkey and pkey != current:
                    # Step 1: Persist the active flag change to providers.json
                    _persisted = self._persist_active_provider(pkey)

                    # Step 2: Re-instantiate the adapter if we have a registered entry
                    existing_adapter = self._providers.get(pkey)
                    if existing_adapter is None:
                        # Try to build adapter from providers.json config
                        new_adapter = self._build_adapter_for_provider(pkey)
                        if new_adapter is not None:
                            self._providers[pkey] = new_adapter
                            existing_adapter = new_adapter
                            _log.info(
                                "model.routing: instantiated new adapter for provider %r",
                                pkey,
                            )

                    switched_provider = pkey if existing_adapter is not None else None

            # --- Update default_model on the (now active) adapter -------------
            target_pkey = switched_provider or canonical_provider(provider_key or "")
            adapter = self._providers.get(target_pkey) if target_pkey else None
            if adapter is None:
                adapter = self.get_active_adapter()
            if adapter is not None and hasattr(adapter, "default_model"):
                try:
                    adapter.default_model = selected_model
                except Exception:
                    pass

            # TASK-21: Persist selected model override to UserPrefs when using bundled defaults
            try:
                cfg_path = _resolve_config_path_dynamic(self.providers_config_path)
                if _is_bundled_providers_config(cfg_path):
                    prefs = _load_user_prefs_dynamic()
                    prefs.selected_model_name = selected_model
                    if target_pkey:
                        prefs.selected_model_provider = target_pkey
                    prefs.save()
            except Exception:
                pass

            _log.info(
                "model.routing: live-switched to model=%r provider=%r (persisted=%s)",
                selected_model,
                provider_key,
                switched_provider is not None,
            )

            # --- Publish completion event -------------------------------------
            if self._event_bus:
                try:
                    self._event_bus.publish_typed(ModelRoutingComplete(model=selected_model, provider=provider_key, switched_provider=switched_provider))
                except Exception:
                    pass
        except Exception:
            pass

    def _persist_active_provider(self, target_key: str) -> bool:
        """Persist active provider selection.

        When targeting the tracked bundled ``src/config/providers.json``,
        writes exclusively to ``UserPrefs`` (``~/.codingagent/prefs.json``) so
        the repository stays clean.  When an explicit custom
        ``providers_config_path`` is configured, updates that file atomically
        via ``tempfile.mkstemp`` + ``os.replace``.
        """
        try:
            cfg_path = _resolve_config_path_dynamic(self.providers_config_path)
            if _is_bundled_providers_config(cfg_path):
                with _providers_json_lock:
                    prefs = _load_user_prefs_dynamic()
                    prefs.selected_model_provider = target_key
                    prefs.save()
                return True
            with _providers_json_lock:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
                changed = False
                for p in providers:
                    if not isinstance(p, dict):
                        continue
                    pkey = canonical_provider(p.get("name") or p.get("type") or "")
                    should_be_active = pkey == target_key
                    if p.get("active") != should_be_active:
                        p["active"] = should_be_active
                        changed = True
                if changed:
                    new_text = json.dumps(
                        providers if isinstance(raw, list) else providers[0], indent=2
                    )
                    fd, tmp = tempfile.mkstemp(dir=cfg_path.parent, suffix=".tmp")
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            fh.write(new_text)
                        os.replace(tmp, cfg_path)
                    except Exception:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        raise
            return True
        except Exception as exc:
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "model.routing: failed to persist active provider: %s", exc
            )
            return False

    def _build_adapter_for_provider(self, target_key: str) -> Optional[Any]:
        """Load adapter class and instantiate adapter for *target_key* from providers.json."""
        try:
            cfg_path = _resolve_config_path_dynamic(self.providers_config_path)
            with _providers_json_lock:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for p in providers:
                if not isinstance(p, dict):
                    continue
                pkey = canonical_provider(p.get("name") or p.get("type") or "")
                if pkey != target_key:
                    continue
                provider_type = str(p.get("type") or "ollama").strip().lower().replace("-", "_")
                adapter_cls, err = _resolve_adapter_class(
                    provider_type=provider_type,
                    camelize=_camelize,
                )
                if err or adapter_cls is None:
                    return None
                adapter, a_err = _instantiate_adapter(
                    adapter_cls=adapter_cls,
                    provider=p,
                    providers_config_path=self.providers_config_path,
                    normalize_models_for_provider=normalize_models_for_provider,
                )
                if adapter is not None:
                    _attach_provider_metadata(adapter, p)
                return adapter
        except Exception:
            pass
        return None

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
                    provider_attr.get(
                        "supports_native_tools",
                        getattr(adapter, "supports_native_tools", False),
                    )
                )
            elif getattr(adapter, "supports_native_tools", False) is True:
                capabilities["supports_native_tools"] = True

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
                "gemini": "gemini",
                "google": "gemini",
                "openai": "openai",
                "openrouter": "openai",
                "groq": "openai",
                "github_copilot": "openai",
                "copilot": "openai",
                "ollama": "local",
                "lm_studio": "local",
                "lmstudio": "local",
                "llama_server": "local",
                "mlx": "local",
                "mlx_lm": "local",
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
            lock=_providers_json_lock,  # type: ignore[arg-type]
        )

    async def initialize(self, force: bool = False, probe: bool = True):
        if self._initialized and not force:
            return
        with self._init_lock:
            if self._initialized and not force:
                return
            if force:
                self._providers.clear()
                self._models_cache.clear()
            guilogger.info("ProviderManager.initialize: loading providers.json")
            cfg = _resolve_config_path_dynamic(self.providers_config_path)
            try:
                if not cfg.exists():
                    # No providers.json present; publish missing event and mark initialized
                    if self._event_bus:
                        try:
                            self._event_bus.publish_typed(ProviderConfigMissing(path=str(cfg)))
                        except Exception:
                            pass
                    self._initialized = True
                    return

                raw = json.loads(cfg.read_text(encoding="utf-8"))
                load_entries = _lm_attr("_load_provider_entries", _load_provider_entries)
                load_registered = _lm_attr(
                    "_load_registered_providers", _load_registered_providers
                )
                providers = load_entries(raw)
                if _is_bundled_providers_config(cfg):
                    providers = _apply_user_prefs_to_providers(
                        providers, canonical_provider
                    )
                self._last_loaded_providers = providers
                load_registered(
                    providers=providers,
                    providers_map=self._providers,
                    provider_models_cache=self._models_cache,
                    module_models_cache=_MODEL_CACHE,
                    module_models_cache_time=_MODEL_CACHE_TIME,
                    model_cache_lock=_MODEL_CACHE_LOCK,  # type: ignore[arg-type]
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

                if probe:
                    self.probe_providers(providers)

            except Exception as e:
                guilogger.error(f"ProviderManager.initialize error: {e}")
            self._initialized = True

    def probe_providers(self, providers: Optional[List[Dict[str, Any]]] = None) -> None:
        """Run the HTTP probe cycle for loaded providers (safe to run in a worker thread)."""
        entries = providers if providers is not None else getattr(self, "_last_loaded_providers", None)
        if entries is None:
            cfg = _resolve_config_path_dynamic(self.providers_config_path)
            if not cfg.exists():
                return
            raw = json.loads(cfg.read_text(encoding="utf-8"))
            load_entries = _lm_attr("_load_provider_entries", _load_provider_entries)
            entries = load_entries(raw)
        run_probe_cycle = _lm_attr("_run_provider_probe_cycle", _run_provider_probe_cycle)
        run_probe_cycle(
            providers=entries,
            providers_map=self._providers,
            provider_models_cache=self._models_cache,
            module_models_cache=_MODEL_CACHE,
            module_models_cache_time=_MODEL_CACHE_TIME,
            model_cache_lock=_MODEL_CACHE_LOCK,  # type: ignore[arg-type]
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
            set_active_context_length_fn=_set_active_context_length_lazy,
            is_active_provider_fn=lambda provider_key, provider_config: (
                provider_key == self.get_active_provider_name()
                if self.get_active_provider_name()
                else bool(provider_config and provider_config.get("active"))
            ),
        )

    async def reload(self) -> None:
        """Force reloading providers.json even if already initialized."""
        await self.initialize(force=True)

    async def validate_provider(self, name: str) -> bool:
        return await _validate_provider_connection(adapter=self.get_provider(name))

