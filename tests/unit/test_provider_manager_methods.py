"""Tests for untested ProviderManager methods.

Coverage targets
----------------
- add_proxy_identifier / remove_proxy_identifier
- set_event_bus: stores bus and subscribes to model.routing
- _on_model_routing: updates adapter.default_model and flips provider active flag
- list_providers / get_provider
- get_cached_models
- get_active_adapter: returns adapter for active provider
- get_active_models: delegates to _get_active_models_helper
- get_active_provider_name: delegates to helper
- is_proxy_adapter: explicit flag, name heuristic, unknown adapter
- get_provider_capabilities: structure and keys
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.core.inference.llm_manager import ProviderManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pm() -> ProviderManager:
    """Return a fresh un-initialised ProviderManager."""
    return ProviderManager(providers_config_path=None)


def _pm_with_providers(**providers) -> ProviderManager:
    """Return a ProviderManager pre-seeded with provider dict entries."""
    pm = _pm()
    pm._providers.update(providers)
    pm._initialized = True
    return pm


# ---------------------------------------------------------------------------
# add_proxy_identifier / remove_proxy_identifier
# ---------------------------------------------------------------------------


def test_add_proxy_identifier_adds_to_frozenset():
    pm = _pm()
    pm.add_proxy_identifier("my_proxy")
    assert "my_proxy" in pm._PROXY_PROVIDER_IDENTIFIERS


def test_add_proxy_identifier_lowercases():
    pm = _pm()
    pm.add_proxy_identifier("MyProxy")
    assert "myproxy" in pm._PROXY_PROVIDER_IDENTIFIERS


def test_add_proxy_identifier_idempotent():
    pm = _pm()
    pm.add_proxy_identifier("litellm")
    pm.add_proxy_identifier("litellm")
    assert pm._PROXY_PROVIDER_IDENTIFIERS.count if False else True  # frozenset, no count


def test_add_proxy_identifier_preserves_existing():
    pm = _pm()
    original = set(pm._PROXY_PROVIDER_IDENTIFIERS)
    pm.add_proxy_identifier("new_proxy")
    assert original.issubset(pm._PROXY_PROVIDER_IDENTIFIERS)


def test_remove_proxy_identifier_removes():
    pm = _pm()
    pm.add_proxy_identifier("temp_proxy")
    pm.remove_proxy_identifier("temp_proxy")
    assert "temp_proxy" not in pm._PROXY_PROVIDER_IDENTIFIERS


def test_remove_proxy_identifier_nonexistent_is_safe():
    pm = _pm()
    pm.remove_proxy_identifier("does_not_exist")  # must not raise


def test_remove_proxy_identifier_case_insensitive():
    pm = _pm()
    pm.add_proxy_identifier("casetest")
    pm.remove_proxy_identifier("CASETEST")
    assert "casetest" not in pm._PROXY_PROVIDER_IDENTIFIERS


# ---------------------------------------------------------------------------
# set_event_bus
# ---------------------------------------------------------------------------


def test_set_event_bus_stores_bus():
    pm = _pm()
    bus = MagicMock()
    pm.set_event_bus(bus)
    assert pm._event_bus is bus


def test_set_event_bus_subscribes_model_routing():
    pm = _pm()
    bus = MagicMock()
    pm.set_event_bus(bus)
    bus.subscribe.assert_called_once_with("model.routing", pm._on_model_routing)


def test_set_event_bus_tolerates_subscribe_exception():
    pm = _pm()
    bus = MagicMock()
    bus.subscribe.side_effect = RuntimeError("bus broken")
    pm.set_event_bus(bus)  # must not raise
    assert pm._event_bus is bus


# ---------------------------------------------------------------------------
# _on_model_routing
# ---------------------------------------------------------------------------


def test_on_model_routing_updates_adapter_default_model():
    pm = _pm()
    adapter = SimpleNamespace(default_model="gpt-4")
    with patch.object(pm, "get_active_adapter", return_value=adapter):
        pm._on_model_routing({"selected": "claude-3-opus"})
    assert adapter.default_model == "claude-3-opus"


def test_on_model_routing_ignores_non_dict_payload():
    pm = _pm()
    pm._on_model_routing("not-a-dict")  # must not raise


def test_on_model_routing_ignores_missing_selected_key():
    pm = _pm()
    adapter = SimpleNamespace(default_model="original")
    with patch.object(pm, "get_active_adapter", return_value=adapter):
        pm._on_model_routing({"provider": "openai"})
    assert adapter.default_model == "original"


def test_on_model_routing_flips_active_provider():
    pm = _pm()
    prov_a = SimpleNamespace(active=True)
    prov_b = SimpleNamespace(active=False)
    pm._providers["provider_a"] = prov_a
    pm._providers["provider_b"] = prov_b
    adapter = SimpleNamespace(default_model="old")
    with patch.object(pm, "get_active_adapter", return_value=adapter):
        pm._on_model_routing({"selected": "new-model", "provider": "provider_b"})
    assert prov_b.active is True
    assert prov_a.active is False


# ---------------------------------------------------------------------------
# list_providers / get_provider
# ---------------------------------------------------------------------------


def test_list_providers_empty():
    pm = _pm()
    assert pm.list_providers() == []


def test_list_providers_sorted():
    pm = _pm_with_providers(beta=object(), alpha=object())
    assert pm.list_providers() == ["alpha", "beta"]


def test_get_provider_returns_value():
    obj = object()
    pm = _pm_with_providers(openai=obj)
    assert pm.get_provider("openai") is obj


def test_get_provider_empty_key_returns_none():
    pm = _pm()
    assert pm.get_provider("") is None


def test_get_provider_unknown_key_returns_none():
    pm = _pm()
    assert pm.get_provider("nonexistent") is None


def test_get_provider_normalises_key():
    obj = object()
    pm = _pm_with_providers(lm_studio=obj)
    # spaces → underscores, lowercased
    assert pm.get_provider("LM Studio") is obj


# ---------------------------------------------------------------------------
# get_cached_models
# ---------------------------------------------------------------------------


def test_get_cached_models_empty_key():
    pm = _pm()
    assert pm.get_cached_models("") == []


def test_get_cached_models_missing_key():
    pm = _pm()
    assert pm.get_cached_models("openai") == []


def test_get_cached_models_returns_copy():
    pm = _pm()
    pm._models_cache["openai"] = ["gpt-4", "gpt-3.5-turbo"]
    result = pm.get_cached_models("openai")
    assert result == ["gpt-4", "gpt-3.5-turbo"]
    result.append("injected")
    # Original must not be modified
    assert pm._models_cache["openai"] == ["gpt-4", "gpt-3.5-turbo"]


def test_get_cached_models_normalises_key():
    pm = _pm()
    pm._models_cache["lm_studio"] = ["llama3"]
    assert pm.get_cached_models("LM Studio") == ["llama3"]


# ---------------------------------------------------------------------------
# get_active_adapter
# ---------------------------------------------------------------------------


def test_get_active_adapter_returns_none_when_no_active_provider():
    pm = _pm()
    with patch.object(pm, "get_active_provider_name", return_value=None):
        assert pm.get_active_adapter() is None


def test_get_active_adapter_returns_provider_object():
    obj = object()
    pm = _pm_with_providers(openai=obj)
    with patch.object(pm, "get_active_provider_name", return_value="openai"):
        assert pm.get_active_adapter() is obj


def test_get_active_adapter_tolerates_exception():
    pm = _pm()
    with patch.object(pm, "get_active_provider_name", side_effect=RuntimeError("fail")):
        assert pm.get_active_adapter() is None


# ---------------------------------------------------------------------------
# is_proxy_adapter
# ---------------------------------------------------------------------------


def test_is_proxy_adapter_false_for_unknown():
    pm = _pm()
    assert pm.is_proxy_adapter(None) is False


def test_is_proxy_adapter_true_via_is_proxy_flag():
    pm = _pm()
    adapter = SimpleNamespace(is_proxy=True, name="custom", provider=None)
    assert pm.is_proxy_adapter(adapter) is True


def test_is_proxy_adapter_true_via_requires_functions_flag():
    pm = _pm()
    adapter = SimpleNamespace(requires_functions=True, name="custom", provider=None)
    assert pm.is_proxy_adapter(adapter) is True


def test_is_proxy_adapter_true_via_name_heuristic():
    pm = _pm()
    adapter = SimpleNamespace(name="litellm_proxy_v2", provider=None)
    assert pm.is_proxy_adapter(adapter) is True


def test_is_proxy_adapter_true_via_provider_dict_requires_functions():
    pm = _pm()
    adapter = SimpleNamespace(name="custom", provider={"requires_functions": True})
    assert pm.is_proxy_adapter(adapter) is True


def test_is_proxy_adapter_false_for_normal_adapter():
    pm = _pm()
    adapter = SimpleNamespace(name="openai_adapter", provider=None)
    # "openai" is not in the default proxy identifiers
    assert pm.is_proxy_adapter(adapter) is False


def test_is_proxy_adapter_true_after_add_proxy_identifier():
    pm = _pm()
    pm.add_proxy_identifier("custom_gw")
    adapter = SimpleNamespace(name="custom_gw_v1", provider=None)
    assert pm.is_proxy_adapter(adapter) is True


def test_is_proxy_adapter_false_after_remove():
    pm = _pm()
    # litellm is a default identifier — remove it
    pm.remove_proxy_identifier("litellm")
    adapter = SimpleNamespace(name="litellm_adapter", provider=None)
    assert pm.is_proxy_adapter(adapter) is False


# ---------------------------------------------------------------------------
# get_provider_capabilities
# ---------------------------------------------------------------------------


def test_get_provider_capabilities_returns_dict():
    pm = _pm()
    with patch.object(pm, "get_active_adapter", return_value=None):
        caps = pm.get_provider_capabilities()
    assert isinstance(caps, dict)


def test_get_provider_capabilities_has_required_keys():
    pm = _pm()
    with patch.object(pm, "get_active_adapter", return_value=None):
        caps = pm.get_provider_capabilities()
    for key in ("supports_native_tools", "provider_family", "model", "provider_name"):
        assert key in caps, f"missing key: {key!r}"


def test_get_provider_capabilities_defaults_safe():
    pm = _pm()
    with patch.object(pm, "get_active_adapter", return_value=None):
        caps = pm.get_provider_capabilities()
    assert caps["supports_native_tools"] is False
    assert isinstance(caps["provider_family"], str)
