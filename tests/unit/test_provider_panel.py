import pytest
from src.ui.views.provider_panel import ProviderPanelController
from src.core.orchestration.event_bus import EventBus
from src.core.inference.llm_manager import _provider_manager


@pytest.fixture(autouse=True)
def restore_provider_manager():
    """Save and restore global _provider_manager state between tests."""
    saved_providers = dict(_provider_manager._providers)
    saved_initialized = _provider_manager._initialized
    yield
    _provider_manager._providers = saved_providers
    _provider_manager._initialized = saved_initialized


def test_provider_panel_receives_models_event(tmp_path, monkeypatch):
    # prepare provider manager with fake provider
    pm = _provider_manager
    pm._providers = {"lm_studio": object()}
    pm._initialized = True
    bus = EventBus()
    pc = ProviderPanelController(bus)
    assert isinstance(pc.list_providers(), list)
    # simulate models list event
    bus.publish("provider.models.list", {"provider": "lm_studio", "models": ["m1"]})
    assert "lm_studio" in pc.list_providers()
