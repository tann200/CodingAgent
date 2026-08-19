"""
Tests for MessageBus — use-case-mapped test suite.

USE-CASE TABLE
==============
UC-BUS-01  Single subscriber receives event                 publish → subscribe
UC-BUS-02  Multiple subscribers receive same event          broadcast fanout
UC-BUS-03  Type isolation: handlers receive only their type event type routing
UC-BUS-04  Unsubscribe stops future delivery                subscription lifecycle
UC-BUS-05  Failed handler isolated; others continue         error isolation
UC-BUS-06  Queue full drops events; metric incremented      backpressure
UC-BUS-07  publish_sync blocks until handler completes      synchronous delivery
UC-BUS-08  publish_sync returns False when queue full       sync timeout
UC-BUS-09  Graceful shutdown drains in-flight events        shutdown correctness
UC-BUS-10  Post-shutdown publishes are discarded            shutdown safety
UC-BUS-11  published / delivered metrics tracked            observability
UC-BUS-12  handler_failed metric tracked                    failure observability
UC-BUS-13  Delivery latency recorded in metrics             latency observability
UC-BUS-14  reset_metrics clears all counters                test utility / ops
UC-BUS-15  Concurrent publishes from many threads           thread safety
UC-BUS-16  Concurrent subscribe/unsubscribe                 thread safety
UC-BUS-17  Correlation ID preserved end-to-end              tracing
UC-BUS-18  Multiple worker threads process events           scalability
"""

import threading
import time
from dataclasses import dataclass
from typing import List

import pytest

from src.core.messaging.bus import MessageBus
from src.core.messaging.events import Event


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@dataclass
class SampleEvent(Event):
    """Generic test event."""
    message: str


@dataclass
class OtherEvent(Event):
    """Second event type for routing tests."""
    value: int


class CollectingHandler:
    """Collects events; signals a threading.Event when N events received."""

    def __init__(self, expected: int = 1):
        self.events: List[Event] = []
        self._done = threading.Event()
        self._expected = expected

    def handle(self, event: Event) -> None:
        self.events.append(event)
        if len(self.events) >= self._expected:
            self._done.set()

    def wait(self, timeout: float = 2.0) -> bool:
        """Block until expected events received or timeout."""
        return self._done.wait(timeout=timeout)

    @property
    def call_count(self) -> int:
        return len(self.events)


class FailingHandler:
    """Always raises; used for isolation tests."""

    def __init__(self):
        self._called = threading.Event()

    def handle(self, event: Event) -> None:
        self._called.set()
        raise ValueError("intentional handler failure")

    def wait(self, timeout: float = 2.0) -> bool:
        return self._called.wait(timeout=timeout)


@pytest.fixture
def bus():
    b = MessageBus(max_queue_size=200, worker_threads=1, enable_metrics=True)
    yield b
    b.shutdown(timeout=3.0)


# ---------------------------------------------------------------------------
# UC-BUS-01  Single subscriber receives event
# ---------------------------------------------------------------------------

class TestSingleSubscriber:
    """UC-BUS-01: An event published to the bus reaches its subscriber."""

    def test_handler_called_once(self, bus):
        """UC-BUS-01: Subscriber handle() called exactly once per publish."""
        h = CollectingHandler(expected=1)
        bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="hello"))
        assert h.call_count == 1

    def test_handler_receives_correct_payload(self, bus):
        """UC-BUS-01: Event payload is preserved end-to-end."""
        h = CollectingHandler()
        bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="payload-check"))
        assert h.events[0].message == "payload-check"


# ---------------------------------------------------------------------------
# UC-BUS-02  Multiple subscribers receive same event
# ---------------------------------------------------------------------------

class TestBroadcastFanout:
    """UC-BUS-02: All subscribers for the same event type receive it."""

    def test_two_handlers_both_called(self, bus):
        """UC-BUS-02: Both handlers receive the broadcast event."""
        h1 = CollectingHandler()
        h2 = CollectingHandler()
        bus.subscribe(SampleEvent, h1)
        bus.subscribe(SampleEvent, h2)
        bus.publish_sync(SampleEvent(message="broadcast"))
        assert h1.call_count == 1
        assert h2.call_count == 1

    def test_three_handlers_all_called(self, bus):
        """UC-BUS-02: N handlers all receive the same event."""
        handlers = [CollectingHandler() for _ in range(3)]
        for h in handlers:
            bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="fan"))
        assert all(h.call_count == 1 for h in handlers)


# ---------------------------------------------------------------------------
# UC-BUS-03  Type isolation: handlers receive only their type
# ---------------------------------------------------------------------------

class TestTypeIsolation:
    """UC-BUS-03: A handler subscribed to EventA does not receive EventB."""

    def test_sample_handler_not_called_for_other_event(self, bus):
        """UC-BUS-03: SampleEvent handler silent when OtherEvent published."""
        sample_h = CollectingHandler()
        other_h = CollectingHandler()
        bus.subscribe(SampleEvent, sample_h)
        bus.subscribe(OtherEvent, other_h)
        bus.publish_sync(SampleEvent(message="x"))
        bus.publish_sync(OtherEvent(value=42))
        assert sample_h.call_count == 1
        assert other_h.call_count == 1
        assert isinstance(sample_h.events[0], SampleEvent)
        assert isinstance(other_h.events[0], OtherEvent)

    def test_no_cross_contamination(self, bus):
        """UC-BUS-03: Publishing OtherEvent does not call SampleEvent handler."""
        sample_h = CollectingHandler()
        bus.subscribe(SampleEvent, sample_h)
        bus.publish_sync(OtherEvent(value=99))
        assert sample_h.call_count == 0


# ---------------------------------------------------------------------------
# UC-BUS-04  Unsubscribe stops future delivery
# ---------------------------------------------------------------------------

class TestUnsubscribe:
    """UC-BUS-04: After unsubscribe, the handler no longer receives events."""

    def test_handler_not_called_after_unsubscribe(self, bus):
        """UC-BUS-04: Unsubscribed handler receives no further events."""
        h = CollectingHandler(expected=1)
        bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="before"))
        assert h.call_count == 1

        bus.unsubscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="after"))
        assert h.call_count == 1  # still 1, second event not delivered

    def test_other_handlers_unaffected_by_unsubscribe(self, bus):
        """UC-BUS-04: Unsubscribing h1 does not affect h2."""
        h1 = CollectingHandler(expected=1)
        h2 = CollectingHandler(expected=2)
        bus.subscribe(SampleEvent, h1)
        bus.subscribe(SampleEvent, h2)
        bus.publish_sync(SampleEvent(message="first"))

        bus.unsubscribe(SampleEvent, h1)
        bus.publish_sync(SampleEvent(message="second"))

        assert h1.call_count == 1
        assert h2.call_count == 2


# ---------------------------------------------------------------------------
# UC-BUS-05  Failed handler isolated; others continue
# ---------------------------------------------------------------------------

class TestHandlerErrorIsolation:
    """UC-BUS-05: An exception in one handler does not prevent others from running."""

    def test_good_handler_runs_after_failing_handler(self, bus):
        """UC-BUS-05: Exception in handler-1 does not block handler-2."""
        failing = FailingHandler()
        good = CollectingHandler()
        bus.subscribe(SampleEvent, failing)
        bus.subscribe(SampleEvent, good)
        bus.publish_sync(SampleEvent(message="test"))
        assert failing.wait(timeout=2.0)
        assert good.call_count == 1

    def test_worker_thread_survives_repeated_handler_failures(self, bus):
        """UC-BUS-05: Bus worker stays alive after multiple handler crashes."""
        failing = FailingHandler()
        good = CollectingHandler(expected=3)
        bus.subscribe(SampleEvent, failing)
        bus.subscribe(SampleEvent, good)
        for _ in range(3):
            bus.publish_sync(SampleEvent(message="repeat"))
        assert good.call_count == 3


# ---------------------------------------------------------------------------
# UC-BUS-06  Queue full drops events; metric incremented
# ---------------------------------------------------------------------------

class TestBackpressure:
    """UC-BUS-06: When the queue fills up, events are dropped (not blocked)."""

    def test_dropped_metric_increments_when_queue_full(self):
        """UC-BUS-06: Queue-full condition increments 'dropped' metric."""
        block = threading.Event()
        unblock = threading.Event()

        class BlockingHandler:
            def handle(self, event: Event) -> None:
                block.set()          # signal: worker is now inside handler
                unblock.wait(timeout=5.0)  # wait until test allows exit

        tiny = MessageBus(max_queue_size=1, worker_threads=1, enable_metrics=True)
        try:
            tiny.subscribe(SampleEvent, BlockingHandler())
            # First publish occupies the single worker slot
            tiny.publish(SampleEvent(message="occupies-worker"))
            block.wait(timeout=2.0)  # wait until worker is inside handler

            # Now flood — queue size 1 is full (worker is still blocked), all drop
            for i in range(20):
                tiny.publish(SampleEvent(message=f"drop-{i}"))

            metrics = tiny.get_metrics()
            dropped = metrics.get("dropped", {}).get("SampleEvent", 0)
            assert dropped > 0
        finally:
            unblock.set()   # release handler so shutdown completes cleanly
            tiny.shutdown(timeout=3.0)


# ---------------------------------------------------------------------------
# UC-BUS-07  publish_sync blocks until handler completes
# ---------------------------------------------------------------------------

class TestPublishSync:
    """UC-BUS-07: publish_sync returns only after the handler has been called."""

    def test_handler_called_before_publish_sync_returns(self, bus):
        """UC-BUS-07: Event is delivered synchronously; no sleep needed."""
        h = CollectingHandler()
        bus.subscribe(SampleEvent, h)
        result = bus.publish_sync(SampleEvent(message="sync"), timeout=2.0)
        # Delivery already done when publish_sync returns
        assert result is True
        assert h.call_count == 1

    def test_publish_sync_returns_true_on_success(self, bus):
        """UC-BUS-07: Return value is True when delivery succeeds."""
        h = CollectingHandler()
        bus.subscribe(SampleEvent, h)
        ok = bus.publish_sync(SampleEvent(message="ok"))
        assert ok is True


# ---------------------------------------------------------------------------
# UC-BUS-08  publish_sync returns False when queue full
# ---------------------------------------------------------------------------

class TestPublishSyncTimeout:
    """UC-BUS-08: publish_sync returns False when the queue cannot accept the event."""

    def test_publish_sync_returns_false_when_queue_full(self):
        """UC-BUS-08: Sync publish fails gracefully when queue is full."""
        block = threading.Event()
        unblock = threading.Event()

        class BlockingHandler:
            def handle(self, event: Event) -> None:
                block.set()
                unblock.wait(timeout=5.0)

        tiny = MessageBus(max_queue_size=1, worker_threads=1)
        try:
            tiny.subscribe(SampleEvent, BlockingHandler())
            tiny.publish(SampleEvent(message="fill"))
            block.wait(timeout=2.0)  # wait until worker is inside handler

            ok = tiny.publish_sync(SampleEvent(message="overflow"), timeout=0.1)
            assert ok is False
        finally:
            unblock.set()
            tiny.shutdown(timeout=3.0)


# ---------------------------------------------------------------------------
# UC-BUS-09  Graceful shutdown drains in-flight events
# ---------------------------------------------------------------------------

class TestShutdown:
    """UC-BUS-09: shutdown() waits for all queued events to be processed."""

    def test_shutdown_delivers_all_queued_events(self):
        """UC-BUS-09: All events published before shutdown are delivered."""
        b = MessageBus(max_queue_size=50, worker_threads=1)
        h = CollectingHandler(expected=10)
        b.subscribe(SampleEvent, h)
        for i in range(10):
            b.publish(SampleEvent(message=f"e{i}"))
        b.shutdown(timeout=5.0)
        assert h.call_count == 10


# ---------------------------------------------------------------------------
# UC-BUS-10  Post-shutdown publishes are discarded
# ---------------------------------------------------------------------------

class TestPostShutdownSafety:
    """UC-BUS-10: Publishing after shutdown is a no-op (doesn't raise)."""

    def test_publish_after_shutdown_does_not_crash(self):
        """UC-BUS-10: Post-shutdown publish silently dropped, no exception."""
        b = MessageBus(max_queue_size=10, worker_threads=1)
        h = CollectingHandler()
        b.subscribe(SampleEvent, h)
        b.shutdown(timeout=2.0)
        # Should not raise
        b.publish(SampleEvent(message="too-late"))
        b.publish(SampleEvent(message="also-too-late"))
        # Handler was never called post-shutdown
        assert h.call_count == 0


# ---------------------------------------------------------------------------
# UC-BUS-11  published / delivered metrics tracked
# ---------------------------------------------------------------------------

class TestMetricsTracking:
    """UC-BUS-11: Observability — event counts reflected in metrics."""

    def test_published_count_matches_publish_calls(self, bus):
        """UC-BUS-11: published metric matches number of publish() calls."""
        h = CollectingHandler(expected=3)
        bus.subscribe(SampleEvent, h)
        for msg in ["a", "b", "c"]:
            bus.publish_sync(SampleEvent(message=msg))
        m = bus.get_metrics()
        assert m["published"]["SampleEvent"] == 3

    def test_delivered_count_matches_successful_deliveries(self, bus):
        """UC-BUS-11: delivered metric matches actual handler calls."""
        h = CollectingHandler(expected=2)
        bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="x"))
        bus.publish_sync(SampleEvent(message="y"))
        m = bus.get_metrics()
        assert m["delivered"]["SampleEvent"] == 2

    def test_multiple_event_types_tracked_independently(self, bus):
        """UC-BUS-11: Each event type has its own counter."""
        sh = CollectingHandler(expected=2)
        oh = CollectingHandler(expected=1)
        bus.subscribe(SampleEvent, sh)
        bus.subscribe(OtherEvent, oh)
        bus.publish_sync(SampleEvent(message="1"))
        bus.publish_sync(SampleEvent(message="2"))
        bus.publish_sync(OtherEvent(value=7))
        m = bus.get_metrics()
        assert m["published"]["SampleEvent"] == 2
        assert m["published"]["OtherEvent"] == 1


# ---------------------------------------------------------------------------
# UC-BUS-12  handler_failed metric tracked
# ---------------------------------------------------------------------------

class TestFailureMetrics:
    """UC-BUS-12: handler_failed metric incremented for each handler exception."""

    def test_handler_failed_metric_incremented(self, bus):
        """UC-BUS-12: One handler failure → one entry in handler_failed."""
        failing = FailingHandler()
        bus.subscribe(SampleEvent, failing)
        bus.publish_sync(SampleEvent(message="fail"))
        failing.wait(timeout=2.0)
        m = bus.get_metrics()
        assert m["handler_failed"].get("SampleEvent:FailingHandler", 0) == 1

    def test_handler_failed_counts_per_handler(self, bus):
        """UC-BUS-12: Repeated failures accumulate in the metric."""
        failing = FailingHandler()
        bus.subscribe(SampleEvent, failing)
        for _ in range(3):
            bus.publish_sync(SampleEvent(message="f"))
        m = bus.get_metrics()
        assert m["handler_failed"].get("SampleEvent:FailingHandler", 0) == 3


# ---------------------------------------------------------------------------
# UC-BUS-13  Delivery latency recorded in metrics
# ---------------------------------------------------------------------------

class TestLatencyMetrics:
    """UC-BUS-13: Delivery latency samples appear in p50/p99 metrics."""

    def test_p50_latency_recorded_after_delivery(self, bus):
        """UC-BUS-13: p50_delivery_ms populated after at least one delivery."""
        h = CollectingHandler()
        bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="lat"))
        m = bus.get_metrics()
        key = "SampleEvent:CollectingHandler"
        assert key in m["p50_delivery_ms"]
        assert m["p50_delivery_ms"][key] >= 0


# ---------------------------------------------------------------------------
# UC-BUS-14  reset_metrics clears all counters
# ---------------------------------------------------------------------------

class TestResetMetrics:
    """UC-BUS-14: reset_metrics() zeroes all counters (ops / test isolation)."""

    def test_reset_clears_published(self, bus):
        """UC-BUS-14: published counter cleared after reset."""
        bus.publish_sync(SampleEvent(message="x"))
        assert bus.get_metrics()["published"]["SampleEvent"] == 1
        bus.reset_metrics()
        assert bus.get_metrics()["published"] == {}

    def test_reset_clears_delivered(self, bus):
        """UC-BUS-14: delivered counter cleared after reset."""
        h = CollectingHandler()
        bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="x"))
        bus.reset_metrics()
        assert bus.get_metrics()["delivered"] == {}

    def test_metrics_accumulate_after_reset(self, bus):
        """UC-BUS-14: New events counted fresh after reset."""
        h = CollectingHandler(expected=2)
        bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="before-reset"))
        bus.reset_metrics()
        bus.publish_sync(SampleEvent(message="after-reset"))
        assert bus.get_metrics()["published"]["SampleEvent"] == 1


# ---------------------------------------------------------------------------
# UC-BUS-15  Concurrent publishes from many threads
# ---------------------------------------------------------------------------

class TestConcurrentPublish:
    """UC-BUS-15: Thread-safe publishing from multiple threads simultaneously."""

    def test_all_events_delivered_under_concurrent_load(self, bus):
        """UC-BUS-15: 50 concurrent publishes (5 threads × 10) all delivered."""
        expected = 50
        h = CollectingHandler(expected=expected)
        bus.subscribe(SampleEvent, h)

        def publish_batch():
            for i in range(10):
                bus.publish(SampleEvent(message=f"t{threading.current_thread().name}-{i}"))

        threads = [threading.Thread(target=publish_batch) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        h.wait(timeout=5.0)
        assert h.call_count == expected


# ---------------------------------------------------------------------------
# UC-BUS-16  Concurrent subscribe/unsubscribe
# ---------------------------------------------------------------------------

class TestConcurrentSubscription:
    """UC-BUS-16: Subscribe and unsubscribe from multiple threads without corruption."""

    def test_no_crash_under_concurrent_subscribe_unsubscribe(self, bus):
        """UC-BUS-16: Race between subscribe and unsubscribe does not crash the bus."""
        def churn():
            h = CollectingHandler()
            for _ in range(20):
                bus.subscribe(SampleEvent, h)
                bus.unsubscribe(SampleEvent, h)

        threads = [threading.Thread(target=churn) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # If we reach here without exception, the test passes


# ---------------------------------------------------------------------------
# UC-BUS-17  Correlation ID preserved end-to-end
# ---------------------------------------------------------------------------

class TestCorrelationId:
    """UC-BUS-17: correlation_id set on publish site is the same on delivery."""

    def test_correlation_id_preserved_through_delivery(self, bus):
        """UC-BUS-17: Subscriber receives the exact correlation_id from publisher."""
        h = CollectingHandler()
        bus.subscribe(SampleEvent, h)
        event = SampleEvent(message="trace-me")
        original_id = event.correlation_id
        bus.publish_sync(event)
        assert h.events[0].correlation_id == original_id

    def test_each_event_gets_unique_correlation_id(self, bus):
        """UC-BUS-17: Two independently constructed events have different IDs."""
        h = CollectingHandler(expected=2)
        bus.subscribe(SampleEvent, h)
        bus.publish_sync(SampleEvent(message="a"))
        bus.publish_sync(SampleEvent(message="b"))
        ids = [e.correlation_id for e in h.events]
        assert ids[0] != ids[1]


# ---------------------------------------------------------------------------
# UC-BUS-18  Multiple worker threads process events
# ---------------------------------------------------------------------------

class TestMultipleWorkers:
    """UC-BUS-18: Bus with multiple worker threads delivers all events."""

    def test_multi_worker_delivers_all_events(self):
        """UC-BUS-18: 4 workers process 100 events without loss."""
        b = MessageBus(max_queue_size=200, worker_threads=4, enable_metrics=True)
        expected = 100
        h = CollectingHandler(expected=expected)
        b.subscribe(SampleEvent, h)
        try:
            for i in range(expected):
                b.publish(SampleEvent(message=f"e{i}"))
            h.wait(timeout=10.0)
            assert h.call_count == expected
        finally:
            b.shutdown(timeout=5.0)
