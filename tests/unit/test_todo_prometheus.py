import os
import sys

from unittest import mock


def test_prometheus_enabled_and_increment(tmp_path, monkeypatch):
    # Ensure metric enabling env var is set
    monkeypatch.setenv("TODO_PROMETHEUS_ENABLED", "1")

    # Create a fake prometheus_client with a Counter class
    class FakeCounter:
        def __init__(self, name, desc):
            self.name = name
            self.count = 0

        def inc(self, amt=1):
            self.count += int(amt)

    fake_module = mock.MagicMock()
    fake_module.Counter = FakeCounter

    monkeypatch.setitem(sys.modules, "prometheus_client", fake_module)

    # Import the wrapper and exercise inc_metric
    from src.tools import todo_metrics

    # Initialize and increment a known metric
    todo_metrics._init()
    todo_metrics.inc_metric("fallback_acquisitions", 2)

    # Verify the underlying fake counter incremented
    c = todo_metrics._counters.get("fallback_acquisitions")
    assert c is not None
    assert c.count >= 2
