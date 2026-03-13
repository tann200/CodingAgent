from pathlib import Path
from types import SimpleNamespace

from src.core.orchestration.event_bus import EventBus
from src.core.telemetry.consumer import TelemetryConsumer


def test_telemetry_consumer_writes_jsonl(tmp_path):
    bus = EventBus()
    out = tmp_path / 'telemetry.jsonl'
    consumer = TelemetryConsumer(bus, out)

    # publish events
    bus.publish('message.truncation', {'dropped_count': 2, 'dropped_tokens': 100, 'tokens_after': 5})
    bus.publish('model.routing', {'provider': 'x', 'available_models': ['a','b'], 'selected': 'b'})

    # read file and assert two lines
    text = out.read_text(encoding='utf-8')
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) >= 2
    import json
    rec = json.loads(lines[0])
    assert rec.get('event') in ('message.truncation', 'model.routing')

