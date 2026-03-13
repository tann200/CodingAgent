from src.core.orchestration.event_bus import EventBus
from src.core.telemetry.metrics import TelemetryMetrics


def test_metrics_counters_and_export(tmp_path):
    bus = EventBus()
    metrics = TelemetryMetrics(bus)

    # publish some events
    bus.publish('message.truncation', {'dropped_count': 2, 'dropped_tokens': 100, 'tokens_after': 5})
    bus.publish('model.routing', {'provider': 'p', 'selected': 'large-70b'})
    bus.publish('tool.execute.start', {'tool': 'write_file'})
    bus.publish('tool.execute.finish', {'tool': 'write_file'})
    bus.publish('tool.execute.error', {'tool': 'write_file'})
    bus.publish('tool.preflight', {'details': {'validated_args': True}})

    assert metrics.get_counter('message_truncation_total') >= 1
    assert metrics.get_counter('model_routing_total') >= 1
    assert metrics.get_counter('tool_execute_start_total') >= 1
    assert metrics.get_counter('tool_execute_finish_total') >= 1
    assert metrics.get_counter('tool_execute_error_total') >= 1

    txt = metrics.export_text()
    assert 'message_truncation_total' in txt
    assert 'model_routing_total' in txt

