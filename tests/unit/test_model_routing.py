"""Tests for P3-3: live model switching via model.routing event."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.core.inference.llm_manager import ProviderManager


def _make_manager_with_bus() -> tuple[ProviderManager, MagicMock]:
    """Create a ProviderManager with a mock event bus and subscribe it."""
    pm = ProviderManager()
    bus = MagicMock()
    # Capture the subscriber so we can call it directly in tests
    subscribed = {}

    def _subscribe(event, handler):
        subscribed[event] = handler

    bus.subscribe.side_effect = _subscribe
    pm.set_event_bus(bus)
    return pm, bus, subscribed


class TestModelRoutingSubscription:
    def test_set_event_bus_subscribes_to_model_routing(self):
        pm = ProviderManager()
        bus = MagicMock()
        pm.set_event_bus(bus)
        # Should have called bus.subscribe with "model.routing"
        calls = [c[0][0] for c in bus.subscribe.call_args_list]
        assert "model.routing" in calls

    def test_set_event_bus_stores_bus(self):
        pm = ProviderManager()
        bus = MagicMock()
        pm.set_event_bus(bus)
        assert pm._event_bus is bus

    def test_subscribe_failure_does_not_raise(self):
        pm = ProviderManager()
        bus = MagicMock()
        bus.subscribe.side_effect = RuntimeError("bus broken")
        pm.set_event_bus(bus)  # Must not raise


class TestOnModelRouting:
    def _pm_with_mock_adapter(self, model_name: str = "gpt-4o"):
        """Build a ProviderManager whose active adapter has a mutable default_model."""
        pm = ProviderManager()
        adapter = MagicMock()
        adapter.default_model = model_name
        # Patch get_active_adapter to return our mock
        pm.get_active_adapter = lambda: adapter
        return pm, adapter

    def test_updates_adapter_default_model(self):
        pm, adapter = self._pm_with_mock_adapter("gpt-4o")
        pm._on_model_routing({"selected": "claude-opus-4", "provider": "anthropic"})
        assert adapter.default_model == "claude-opus-4"

    def test_ignores_empty_selected(self):
        pm, adapter = self._pm_with_mock_adapter("gpt-4o")
        pm._on_model_routing({"selected": "", "provider": "openai"})
        # Should not have changed the adapter model
        assert adapter.default_model == "gpt-4o"

    def test_ignores_non_dict_payload(self):
        pm, adapter = self._pm_with_mock_adapter("gpt-4o")
        pm._on_model_routing("not a dict")
        assert adapter.default_model == "gpt-4o"

    def test_ignores_none_payload(self):
        pm, adapter = self._pm_with_mock_adapter("gpt-4o")
        pm._on_model_routing(None)
        assert adapter.default_model == "gpt-4o"

    def test_handles_adapter_without_default_model(self):
        """Adapters that don't expose default_model must not raise."""
        pm = ProviderManager()
        adapter = MagicMock(spec=[])  # no attributes
        pm.get_active_adapter = lambda: adapter
        pm._on_model_routing({"selected": "llama3.2", "provider": "ollama"})
        # Should complete without error

    def test_handles_no_active_adapter(self):
        """None adapter must not raise."""
        pm = ProviderManager()
        pm.get_active_adapter = lambda: None
        pm._on_model_routing({"selected": "mistral", "provider": "openai"})

    def test_does_not_raise_on_exception_inside(self):
        """_on_model_routing must never propagate exceptions."""
        pm = ProviderManager()

        def _raise():
            raise RuntimeError("unexpected crash")

        pm.get_active_adapter = _raise
        pm._on_model_routing({"selected": "gpt-4o"})  # Must not raise

    def test_model_key_alias(self):
        """Accepts 'model' as alias for 'selected'."""
        pm, adapter = self._pm_with_mock_adapter("gpt-4o")
        pm._on_model_routing({"model": "gemini-2.5-pro"})
        assert adapter.default_model == "gemini-2.5-pro"

    def test_provider_key_activates_provider(self):
        """When provider_key matches a provider in _providers, mark it active."""
        pm = ProviderManager()

        # Set up two providers with mutable .active attribute
        provider_a = MagicMock()
        provider_a.active = True
        provider_b = MagicMock()
        provider_b.active = False

        pm._providers = {"openai": provider_a, "anthropic": provider_b}
        pm.get_active_adapter = lambda: None

        pm._on_model_routing({"selected": "claude-3-7", "provider": "anthropic"})

        assert provider_b.active is True
        assert provider_a.active is False

    def test_provider_key_missing_from_providers_does_not_raise(self):
        pm = ProviderManager()
        pm._providers = {}
        pm.get_active_adapter = lambda: None
        pm._on_model_routing({"selected": "gpt-4o", "provider": "unknown_provider"})


class TestIntegration:
    def test_event_bus_routes_to_on_model_routing(self):
        """Full round-trip: set_event_bus → subscriber stored → payload dispatched."""
        pm = ProviderManager()
        adapter = MagicMock()
        adapter.default_model = "gpt-4o"
        pm.get_active_adapter = lambda: adapter

        subscribed_handlers = {}
        bus = MagicMock()
        bus.subscribe.side_effect = lambda event, handler: subscribed_handlers.update({event: handler})

        pm.set_event_bus(bus)

        # Simulate event bus firing the event
        assert "model.routing" in subscribed_handlers
        subscribed_handlers["model.routing"]({"selected": "claude-3-7-sonnet", "provider": "anthropic"})

        assert adapter.default_model == "claude-3-7-sonnet"
