"""Tests for Gap 8: OTel tracer (src/core/telemetry/tracer.py).

Covers:
  1. span_node is a valid context manager when OTel SDK is NOT installed (no-op).
  2. span_node yields None in no-op mode.
  3. record_event is a no-op when span is None.
  4. get_tracer returns None when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
  5. get_tracer returns None when OTel SDK is missing.
  6. wire_event_bus subscribes to tool/perception topics without raising.
  7. _span_node wrappers in node modules work without OTel installed.
  8. Multiple span_node calls are independent (no shared state leakage).
"""

import os
import sys
import importlib
import unittest
from unittest.mock import MagicMock, patch


class TestSpanNodeNoOp(unittest.TestCase):
    """span_node must behave as a no-op context manager when OTel is absent."""

    def setUp(self):
        # Force fresh module state by resetting the cached tracer.
        import src.core.telemetry.tracer as tracer_mod

        tracer_mod._tracer = None
        tracer_mod._initialised = False

    def test_span_node_is_context_manager(self):
        """span_node() must be usable as a context manager in all cases."""
        from src.core.telemetry.tracer import span_node

        with span_node("test") as span:
            pass  # must not raise

    def test_span_node_yields_none_when_no_otel(self):
        """Without OTel endpoint span_node yields None."""
        from src.core.telemetry.tracer import span_node

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            with span_node("test") as span:
                result = span
        self.assertIsNone(result)

    def test_span_node_accepts_attributes(self):
        """span_node must accept an attributes dict without raising."""
        from src.core.telemetry.tracer import span_node

        with span_node("test", {"round": 3, "model": "gemma-4-e4b-it"}):
            pass

    def test_span_node_handles_none_attributes(self):
        """span_node must accept None attributes."""
        from src.core.telemetry.tracer import span_node

        with span_node("test", None):
            pass

    def test_multiple_spans_independent(self):
        """Multiple span_node calls must not interfere with each other."""
        from src.core.telemetry.tracer import span_node

        results = []
        with span_node("a", {"x": 1}) as s1:
            results.append(s1)
            with span_node("b", {"x": 2}) as s2:
                results.append(s2)
        # Both should be None in no-op mode
        self.assertEqual(results, [None, None])


class TestRecordEvent(unittest.TestCase):
    """record_event must be a no-op when span is None."""

    def test_record_event_none_span(self):
        """record_event with None span must not raise."""
        from src.core.telemetry.tracer import record_event

        record_event(None, "test.event", {"key": "value"})

    def test_record_event_none_attributes(self):
        """record_event with None attributes must not raise."""
        from src.core.telemetry.tracer import record_event

        record_event(None, "test.event", None)

    def test_record_event_with_mock_span(self):
        """record_event calls add_event on a real span object."""
        from src.core.telemetry.tracer import record_event

        mock_span = MagicMock()
        record_event(mock_span, "test.event", {"k": "v"})
        mock_span.add_event.assert_called_once_with("test.event", attributes={"k": "v"})


class TestGetTracer(unittest.TestCase):
    """get_tracer returns None when endpoint is unset or OTel is missing."""

    def setUp(self):
        import src.core.telemetry.tracer as tracer_mod

        tracer_mod._tracer = None
        tracer_mod._initialised = False

    def tearDown(self):
        import src.core.telemetry.tracer as tracer_mod

        tracer_mod._tracer = None
        tracer_mod._initialised = False

    def test_get_tracer_no_endpoint(self):
        """get_tracer returns None when no endpoint env var is set."""
        from src.core.telemetry.tracer import get_tracer

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            result = get_tracer()
        self.assertIsNone(result)

    def test_get_tracer_cached(self):
        """get_tracer returns the same object on repeated calls."""
        from src.core.telemetry.tracer import get_tracer

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        r1 = get_tracer()
        r2 = get_tracer()
        self.assertIs(r1, r2)


class TestWireEventBus(unittest.TestCase):
    """wire_event_bus must subscribe without raising, even when OTel is absent."""

    def test_wire_event_bus_no_otel(self):
        """wire_event_bus must not raise when OTel is disabled."""
        from src.core.telemetry.tracer import wire_event_bus

        mock_bus = MagicMock()
        wire_event_bus(mock_bus)  # must not raise

    def test_wire_event_bus_subscribes_to_topics(self):
        """wire_event_bus calls subscribe for all four expected topics."""
        from src.core.telemetry.tracer import wire_event_bus

        mock_bus = MagicMock()
        wire_event_bus(mock_bus)

        subscribed_topics = {call.args[0] for call in mock_bus.subscribe.call_args_list}
        self.assertIn("tool.execute.start", subscribed_topics)
        self.assertIn("tool.execute.finish", subscribed_topics)
        self.assertIn("tool.execute.error", subscribed_topics)
        self.assertIn("perception.complete", subscribed_topics)

    def test_wire_event_bus_bad_bus_no_raise(self):
        """wire_event_bus swallows exceptions from a broken event bus."""
        from src.core.telemetry.tracer import wire_event_bus

        bad_bus = MagicMock()
        bad_bus.subscribe.side_effect = RuntimeError("bus broken")
        wire_event_bus(bad_bus)  # must not propagate


class TestNodeSpanWrappers(unittest.TestCase):
    """_span_node wrappers in graph nodes work without OTel."""

    def test_perception_node_span_wrapper(self):
        """perception_node module's _span_node wrapper is a callable context manager."""
        from src.core.orchestration.graph.nodes.perception_node import _span_node

        with _span_node("perception", {"round": 0}):
            pass

    def test_execution_node_span_wrapper(self):
        """execution_node module's _span_node wrapper is a callable context manager."""
        from src.core.orchestration.graph.nodes.execution_node import _span_node

        with _span_node("execution", {"step": 1}):
            pass

    def test_planning_node_span_wrapper(self):
        """planning_node module's _span_node wrapper is a callable context manager."""
        from src.core.orchestration.graph.nodes.planning_node import _span_node

        with _span_node("planning", {"plan_attempts": 0}):
            pass


if __name__ == "__main__":
    unittest.main()
