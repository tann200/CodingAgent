"""Tests for MessageBusMetrics."""

from src.core.messaging.metrics import MessageBusMetrics


def test_metrics_initial_state():
    """Verify metrics start at zero."""
    metrics = MessageBusMetrics()
    snapshot = metrics.snapshot()
    
    assert snapshot["published"] == {}
    assert snapshot["delivered"] == {}
    assert snapshot["dropped"] == {}
    assert snapshot["handler_failed"] == {}


def test_increment_published():
    """Verify increment_published increments counter."""
    metrics = MessageBusMetrics()
    
    metrics.increment_published("AgentStarted")
    metrics.increment_published("AgentStarted")
    metrics.increment_published("ToolCallStarted")
    
    snapshot = metrics.snapshot()
    assert snapshot["published"]["AgentStarted"] == 2
    assert snapshot["published"]["ToolCallStarted"] == 1


def test_increment_dropped():
    """Verify increment_dropped increments counter."""
    metrics = MessageBusMetrics()
    
    metrics.increment_dropped("ToolCallStarted")
    
    snapshot = metrics.snapshot()
    assert snapshot["dropped"]["ToolCallStarted"] == 1


def test_increment_handler_failed():
    """Verify increment_handler_failed increments counter."""
    metrics = MessageBusMetrics()
    
    metrics.increment_handler_failed("AgentStarted", "TUIEventHandler")
    metrics.increment_handler_failed("AgentStarted", "TUIEventHandler")
    
    snapshot = metrics.snapshot()
    assert snapshot["handler_failed"]["AgentStarted:TUIEventHandler"] == 2


def test_record_delivery():
    """Verify record_delivery increments delivered and records latency."""
    metrics = MessageBusMetrics()
    
    metrics.record_delivery("AgentStarted", "TUIEventHandler", 10.5)
    metrics.record_delivery("AgentStarted", "TUIEventHandler", 12.3)
    
    snapshot = metrics.snapshot()
    assert snapshot["delivered"]["AgentStarted"] == 2
    assert "AgentStarted:TUIEventHandler" in snapshot["p50_delivery_ms"]


def test_p50_latency_calculation():
    """Verify p50 latency is calculated correctly."""
    metrics = MessageBusMetrics()
    
    # Record 5 samples: [10, 20, 30, 40, 50]
    for latency in [10.0, 20.0, 30.0, 40.0, 50.0]:
        metrics.record_delivery("TestEvent", "TestHandler", latency)
    
    snapshot = metrics.snapshot()
    p50 = snapshot["p50_delivery_ms"]["TestEvent:TestHandler"]
    
    # p50 should be median (30.0)
    assert p50 == 30.0


def test_p99_latency_with_100_samples():
    """Verify p99 latency calculation with 100+ samples."""
    metrics = MessageBusMetrics()
    
    # Record 100 samples: 1, 2, 3, ..., 100
    for i in range(1, 101):
        metrics.record_delivery("TestEvent", "TestHandler", float(i))
    
    snapshot = metrics.snapshot()
    p99 = snapshot["p99_delivery_ms"]["TestEvent:TestHandler"]
    
    # p99 should be around 99
    assert 98 <= p99 <= 100


def test_p99_latency_with_few_samples():
    """Verify p99 latency uses max when < 100 samples."""
    metrics = MessageBusMetrics()
    
    # Record 5 samples
    for latency in [10.0, 20.0, 30.0, 40.0, 50.0]:
        metrics.record_delivery("TestEvent", "TestHandler", latency)
    
    snapshot = metrics.snapshot()
    p99 = snapshot["p99_delivery_ms"]["TestEvent:TestHandler"]
    
    # p99 should be max (50.0) when < 100 samples
    assert p99 == 50.0


def test_reset_clears_all_metrics():
    """Verify reset() clears all counters and samples."""
    metrics = MessageBusMetrics()
    
    # Add some data
    metrics.increment_published("AgentStarted")
    metrics.increment_dropped("ToolCallStarted")
    metrics.record_delivery("AgentStarted", "Handler", 10.0)
    
    # Reset
    metrics.reset()
    
    # Verify all cleared
    snapshot = metrics.snapshot()
    assert snapshot["published"] == {}
    assert snapshot["delivered"] == {}
    assert snapshot["dropped"] == {}
    assert snapshot["handler_failed"] == {}
    assert snapshot["p50_delivery_ms"] == {}
    assert snapshot["p99_delivery_ms"] == {}


def test_snapshot_does_not_mutate_metrics():
    """Verify snapshot() returns copy, not reference."""
    metrics = MessageBusMetrics()
    
    metrics.increment_published("AgentStarted")
    
    snapshot1 = metrics.snapshot()
    snapshot1["published"]["AgentStarted"] = 999
    
    snapshot2 = metrics.snapshot()
    assert snapshot2["published"]["AgentStarted"] == 1  # Not mutated


def test_multiple_event_types():
    """Verify metrics handle multiple event types correctly."""
    metrics = MessageBusMetrics()
    
    metrics.increment_published("AgentStarted")
    metrics.increment_published("AgentCompleted")
    metrics.increment_published("ToolCallStarted")
    metrics.increment_published("AgentStarted")
    
    snapshot = metrics.snapshot()
    assert snapshot["published"]["AgentStarted"] == 2
    assert snapshot["published"]["AgentCompleted"] == 1
    assert snapshot["published"]["ToolCallStarted"] == 1


def test_multiple_handlers():
    """Verify metrics handle multiple handlers correctly."""
    metrics = MessageBusMetrics()
    
    metrics.record_delivery("AgentStarted", "TUIHandler", 10.0)
    metrics.record_delivery("AgentStarted", "AuditLogger", 5.0)
    
    snapshot = metrics.snapshot()
    assert "AgentStarted:TUIHandler" in snapshot["p50_delivery_ms"]
    assert "AgentStarted:AuditLogger" in snapshot["p50_delivery_ms"]
