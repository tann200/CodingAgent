from src.ui.views.main_view import MainViewController
from src.core.orchestration.event_bus import EventBus


def test_main_view_startup_and_provider_status():
    bus = EventBus()
    mv = MainViewController(bus)
    assert mv.get_status() == 'idle'
    bus.publish('orchestrator.startup', {'time': 1})
    assert mv.get_status() == 'started'
    bus.publish('provider.status.changed', {'provider': 'lm_studio', 'status': 'connected'})
    assert mv.state.active_provider == 'lm_studio'

