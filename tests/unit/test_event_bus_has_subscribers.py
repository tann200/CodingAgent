import pytest
from src.core.messaging.bus import MessageBus
from src.core.messaging.event_types import (
    FileDiffPreview,
    ToolPermissionGranted,
    ToolPermissionDenied,
)
from src.core.orchestration.event_bus import EventBus
from tui.tui_src.ui.mock_eventbus import MockEventBus


class DummyHandler:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


def test_event_bus_has_subscribers_legacy_string():
    bus = EventBus()
    assert not bus.has_subscribers("test.event")
    cb = lambda p: None
    bus.subscribe("test.event", cb)
    assert bus.has_subscribers("test.event")
    bus.unsubscribe("test.event", cb)
    assert not bus.has_subscribers("test.event")


def test_event_bus_has_subscribers_typed():
    typed_bus = MessageBus(max_queue_size=10, worker_threads=1)
    bus = EventBus(typed_bus=typed_bus)

    try:
        # Initially no subscribers
        assert not bus.has_subscribers("file.diff.preview")

        handler = DummyHandler()
        typed_bus.subscribe(FileDiffPreview, handler)

        # Now bus.has_subscribers("file.diff.preview") should return True via typed_bus
        assert bus.has_subscribers("file.diff.preview")

        typed_bus.unsubscribe(FileDiffPreview, handler)
        assert not bus.has_subscribers("file.diff.preview")
    finally:
        typed_bus.shutdown(timeout=2.0)


def test_event_bus_tool_permission_events_mapping():
    typed_bus = MessageBus(max_queue_size=10, worker_threads=1)
    bus = EventBus(typed_bus=typed_bus)

    try:
        handler_granted = DummyHandler()
        handler_denied = DummyHandler()
        typed_bus.subscribe(ToolPermissionGranted, handler_granted)
        typed_bus.subscribe(ToolPermissionDenied, handler_denied)

        assert bus.has_subscribers("tool.permission_granted")
        assert bus.has_subscribers("tool.permission_denied")

        bus.publish("tool.permission_granted", {"tool_id": "test_tool_1"})
        bus.publish("tool.permission_denied", {"tool_id": "test_tool_2"})

        typed_bus.shutdown(timeout=2.0)

        assert len(handler_granted.events) == 1
        assert isinstance(handler_granted.events[0], ToolPermissionGranted)
        assert handler_granted.events[0].tool_id == "test_tool_1"

        assert len(handler_denied.events) == 1
        assert isinstance(handler_denied.events[0], ToolPermissionDenied)
        assert handler_denied.events[0].tool_id == "test_tool_2"
    finally:
        try:
            typed_bus.shutdown(timeout=1.0)
        except Exception:
            pass


def test_mock_event_bus_has_subscribers():
    mock_bus = MockEventBus()
    assert not mock_bus.has_subscribers("file.diff.preview")
    cb = lambda p: None
    mock_bus.subscribe("file.diff.preview", cb)
    assert mock_bus.has_subscribers("file.diff.preview")
    mock_bus.unsubscribe("file.diff.preview", cb)
    assert not mock_bus.has_subscribers("file.diff.preview")
