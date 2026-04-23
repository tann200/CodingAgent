"""tests/unit/test_telemetry_events.py

Consolidated telemetry tests.  Replaces four single-test files:
  - test_app_telemetry.py    (TelemetryConsumer writes events to JSONL)
  - test_log_panel.py        (log.new event delivered to subscribers)
  - test_metrics.py          (TelemetryMetrics counters and text export)
  - test_telemetry_publish.py (AdapterWrapper publishes model.response event)
"""


# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# TelemetryConsumer — writes events to JSONL file
# ---------------------------------------------------------------------------

def test_telemetry_consumer_writes_event(tmp_path):
    """Publishing an event on the bus writes it to the telemetry JSONL file."""
    from src.core.orchestration.event_bus import EventBus
    from src.core.telemetry.consumer import TelemetryConsumer

    bus = EventBus()
    telemetry_path = tmp_path / "telemetry_app.jsonl"
    consumer = TelemetryConsumer(bus, telemetry_path)  # noqa: F841 — must stay alive
    bus.publish(
        "message.truncation",
        {"dropped_count": 1, "dropped_tokens": 10, "tokens_after": 5},
    )
    assert telemetry_path.exists(), "Telemetry file must be created after first event"
    text = telemetry_path.read_text(encoding="utf-8")
    assert "message.truncation" in text


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


# ---------------------------------------------------------------------------
# TelemetryMetrics — counters and text export
# ---------------------------------------------------------------------------

def test_metrics_counters_and_export(tmp_path):
    from src.core.orchestration.event_bus import EventBus
    from src.core.telemetry.metrics import TelemetryMetrics

    bus = EventBus()
    metrics = TelemetryMetrics(bus)

    bus.publish(
        "message.truncation",
        {"dropped_count": 2, "dropped_tokens": 100, "tokens_after": 5},
    )
    bus.publish("model.routing", {"provider": "p", "selected": "large-70b"})
    bus.publish(
        "tool.execute.start",
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_123",
            "title": "write_file",
            "status": "in_progress",
            "rawInput": {},
        },
    )
    bus.publish(
        "tool.execute.finish",
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_123",
            "title": "write_file",
            "status": "completed",
            "content": [],
            "rawOutput": {},
        },
    )
    bus.publish(
        "tool.execute.error",
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_123",
            "title": "write_file",
            "status": "failed",
            "error": "test error",
        },
    )

    assert metrics.get_counter("message_truncation_total") >= 1
    assert metrics.get_counter("model_routing_total") >= 1
    assert metrics.get_counter("tool_execute_start_total") >= 1
    assert metrics.get_counter("tool_execute_finish_total") >= 1
    assert metrics.get_counter("tool_execute_error_total") >= 1

    txt = metrics.export_text()
    assert "message_truncation_total" in txt
    assert "model_routing_total" in txt


# ---------------------------------------------------------------------------
# AdapterWrapper — publishes model.response telemetry event
# ---------------------------------------------------------------------------

def test_publish_model_response_on_wrapper():
    from src.core.inference.adapter_wrappers import AdapterWrapper

    class _DummyBus:
        def __init__(self):
            self.events: list = []

        def publish(self, name, payload):
            self.events.append((name, payload))

    class _MockAdapter:
        def chat(self, messages, model=None, stream=False, format_json=False, **kwargs):
            return {
                "model": model or "test-model",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }

    bus = _DummyBus()
    wrapper = AdapterWrapper(_MockAdapter(), provider_name="lm_studio", event_bus=bus)
    out = wrapper.generate([{"role": "user", "content": "hi"}], model="test-model")

    assert out.get("ok") is True
    assert any(e[0] == "model.response" for e in bus.events)
    evt = next(e for e in bus.events if e[0] == "model.response")
    for key in ("provider", "model", "prompt_tokens", "completion_tokens", "latency"):
        assert key in evt[1], f"Missing key '{key}' in model.response payload"
