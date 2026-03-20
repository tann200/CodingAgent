
import json
from pathlib import Path

from src.core.orchestration.event_bus import EventBus
from src.core.telemetry.consumer import TelemetryConsumer


def test_telemetry_consumer_writes_jsonl(tmp_path):
    bus = EventBus()
    out = tmp_path / 'telemetry.jsonl'
    _consumer = TelemetryConsumer(bus, out)

    # publish events
    bus.publish('message.truncation', {'dropped_count': 2, 'dropped_tokens': 100, 'tokens_after': 5})
    bus.publish('model.routing', {'provider': 'x', 'available_models': ['a','b'], 'selected': 'b'})

    # read file and assert two lines
    text = out.read_text(encoding='utf-8')
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) >= 2
    rec = json.loads(lines[0])
    assert rec.get('event') in ('message.truncation', 'model.routing')


# ---------------------------------------------------------------------------
# #40: Disk-based telemetry rotation tests
# ---------------------------------------------------------------------------

class TestTelemetryRotation:
    """#40: TelemetryConsumer rotates file when max_bytes is exceeded."""

    def test_no_rotation_below_threshold(self, tmp_path):
        out = tmp_path / "telemetry.jsonl"
        bus = EventBus()
        consumer = TelemetryConsumer(bus, out, max_bytes=10_000, backup_count=3)
        bus.publish("model.routing", {"provider": "x", "selected": "y"})
        assert out.exists()
        assert not Path(f"{out}.1").exists()

    def test_rotation_triggers_when_threshold_exceeded(self, tmp_path):
        out = tmp_path / "telemetry.jsonl"
        # Pre-populate the file so it's already over the tiny threshold
        out.write_text("x" * 100)
        bus = EventBus()
        # max_bytes=50 → file is already 100 bytes → rotate on next write
        consumer = TelemetryConsumer(bus, out, max_bytes=50, backup_count=3)
        bus.publish("model.routing", {"provider": "a", "selected": "b"})
        assert Path(f"{out}.1").exists()
        assert out.exists()  # new file created for the new event

    def test_backup_count_limits_old_files(self, tmp_path):
        out = tmp_path / "telemetry.jsonl"
        bus = EventBus()
        consumer = TelemetryConsumer(bus, out, max_bytes=1, backup_count=2)
        # Each publish will trigger a rotation (max_bytes=1 is tiny)
        for i in range(6):
            out.write_text("x" * 10)  # ensure file exceeds threshold each time
            bus.publish("model.routing", {"provider": "x", "i": i})
        # Only backups .1 and .2 should exist (backup_count=2)
        assert not Path(f"{out}.3").exists()

    def test_rotation_backup_suffix_increments(self, tmp_path):
        out = tmp_path / "telemetry.jsonl"
        out.write_text("x" * 200)
        bus = EventBus()
        consumer = TelemetryConsumer(bus, out, max_bytes=50, backup_count=3)
        bus.publish("model.routing", {"provider": "a", "selected": "b"})
        assert Path(f"{out}.1").exists()
        # Write again to trigger another rotation
        out.write_text("y" * 200)
        bus.publish("model.routing", {"provider": "b", "selected": "c"})
        assert Path(f"{out}.2").exists()

    def test_default_params_accepted(self, tmp_path):
        out = tmp_path / "telemetry.jsonl"
        bus = EventBus()
        # Should not raise; default max_bytes=5MB, backup_count=3
        consumer = TelemetryConsumer(bus, out)
        bus.publish("model.routing", {"provider": "x", "selected": "y"})
        assert out.exists()

