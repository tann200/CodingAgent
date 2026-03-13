from src.ui.components.log_panel import LogPanel
from src.core.orchestration.event_bus import EventBus


def test_log_panel_collects_logs():
    bus = EventBus()
    lp = LogPanel(bus)
    bus.publish('log.new', {'timestamp': '00:00:00', 'level': 'INFO', 'message': 'hello'})
    assert lp.tail(1)[0]['message'] == 'hello'

