from src.ui.views.provider_panel import ProviderPanelController
from src.core.orchestration.event_bus import EventBus
from src.core.llm_manager import _provider_manager


def test_provider_panel_receives_models_event(tmp_path, monkeypatch):
    # prepare provider manager with fake provider
    pm = _provider_manager
    pm._providers = {'lm_studio': object()}
    pm._initialized = True
    bus = EventBus()
    pc = ProviderPanelController(bus)
    assert isinstance(pc.list_providers(), list)
    # simulate models list event
    bus.publish('provider.models.list', {'provider': 'lm_studio', 'models': ['m1']})
    assert 'lm_studio' in pc.list_providers()

