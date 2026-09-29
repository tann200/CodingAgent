"""tests/unit/test_telemetry_events.py

EventBus subscriber delivery.

The former telemetry suite here (``TelemetryConsumer`` JSONL sink,
``TelemetryMetrics`` counters, and ``AdapterWrapper`` model.response
publication) was consolidated from four single-test files.  All three
components were unreachable from production code and have been removed, so
only the EventBus subscriber test below remains.  Telemetry on the live path
is covered by ``test_messaging.py`` and the ``metrics`` singleton tests.
"""


# ruff: noqa: E501
from __future__ import annotations


# ---------------------------------------------------------------------------
# EventBus subscriber — log.new event delivered
# ---------------------------------------------------------------------------

def test_log_new_event_collected_by_subscriber():
    """Publishing log.new delivers the payload to all subscribers."""
    from src.core.orchestration.event_bus import EventBus

    bus = EventBus()
    collected: list[dict] = []
    bus.subscribe("log.new", collected.append)
    bus.publish(
        "log.new", {"timestamp": "00:00:00", "level": "INFO", "message": "hello"}
    )
    assert len(collected) == 1
    assert collected[0]["message"] == "hello"
