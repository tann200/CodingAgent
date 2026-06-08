"""
Tests for event_bus_adapter.py — DualPublishBus and related helpers.

Use-case table
==============
UC-ADP-01  DualPublishBus wraps old EventBus and delegates subscribe/unsubscribe
UC-ADP-02  publish() emits typed event on MessageBus for mapped event names
UC-ADP-03  publish() still delivers via old bus (legacy subscribers unaffected)
UC-ADP-04  Field name mapper handles camelCase → snake_case conversion
UC-ADP-05  Unmapped event names only delivered on old bus (no typed emission)
UC-ADP-06  Bad payload (non-dict) doesn't crash — old bus still receives
UC-ADP-07  None payload handled gracefully
UC-ADP-08  _correlation_id in payload filtered from typed event constructor
UC-ADP-09  publish_typed() emits typed event on MessageBus and dict on old bus
UC-ADP-10  get_typed_bus() returns a singleton MessageBus
UC-ADP-11  reset_typed_bus() clears singleton and shuts down the bus
UC-ADP-12  All delegate methods (subscribe, unsubscribe, etc.) forwarded to old bus
UC-ADP-13  DualPublishBus returned by get_event_bus()
"""

import threading
import time

import pytest

from src.core.orchestration.event_bus import EventBus, get_event_bus
from src.core.messaging import (
    DualPublishBus,
    Event,
    GitBranch,
    MessageBus,
    OrchestratorStartup,
    ToolInvoked,
    get_typed_bus,
    reset_typed_bus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class CollectingHandler:
    """MessageBus EventHandler that collects received events."""

    def __init__(self, expected: int = 1):
        self.received: list[Event] = []
        self._event = threading.Event()
        self._expected = expected

    def handle(self, event: Event) -> None:
        self.received.append(event)
        if len(self.received) >= self._expected:
            self._event.set()

    def wait(self, timeout: float = 3.0) -> bool:
        return self._event.wait(timeout=timeout)


@pytest.fixture(autouse=True)
def _reset_bus():
    """Reset typed bus before each test."""
    reset_typed_bus()
    yield
    reset_typed_bus()


@pytest.fixture
def typed_bus() -> MessageBus:
    return get_typed_bus()


@pytest.fixture
def raw_event_bus() -> EventBus:
    """A plain EventBus (not wrapped)."""
    return EventBus()


@pytest.fixture
def adapter(raw_event_bus, typed_bus) -> DualPublishBus:
    return DualPublishBus(raw_event_bus, typed_bus=typed_bus)


# ---------------------------------------------------------------------------
# UC-ADP-01: Construction and delegation
# ---------------------------------------------------------------------------

class TestConstruction:
    """UC-ADP-01: DualPublishBus wraps old EventBus."""

    def test_get_typed_bus_returns_singleton(self):
        """UC-ADP-10: get_typed_bus() returns the same instance."""
        b1 = get_typed_bus()
        b2 = get_typed_bus()
        assert b1 is b2

    def test_reset_typed_bus_clears(self):
        """UC-ADP-11: reset_typed_bus() shuts down and clears."""
        b1 = get_typed_bus()
        reset_typed_bus()
        b2 = get_typed_bus()
        assert b1 is not b2

    def test_get_event_bus_returns_wrapped(self):
        """UC-ADP-13: get_event_bus() returns DualPublishBus."""
        bus = get_event_bus()
        assert isinstance(bus, DualPublishBus)

    def test_subscribe_delegated(self, adapter, raw_event_bus):
        """UC-ADP-01: subscribe on adapter reaches old bus."""
        received = []
        adapter.subscribe("test.event", lambda p: received.append(p))
        raw_event_bus.publish("test.event", {"x": 1})
        assert received == [{"x": 1}]

    def test_unsubscribe_delegated(self, adapter, raw_event_bus):
        """UC-ADP-01: unsubscribe removes from old bus."""
        received = []
        cb = lambda p: received.append(p)
        adapter.subscribe("test.event", cb)
        adapter.unsubscribe("test.event", cb)
        raw_event_bus.publish("test.event", {"x": 1})
        assert received == []

    def test_has_subscribers_delegated(self, adapter):
        """UC-ADP-01: has_subscribers forwarded."""
        cb = lambda p: None
        adapter.subscribe("test.event", cb)
        assert adapter.has_subscribers("test.event") is True


# ---------------------------------------------------------------------------
# UC-ADP-02…05: publish() dual emission
# ---------------------------------------------------------------------------

class TestPublishDualEmission:
    """UC-ADP-02…05: publish sends to both buses."""

    def test_typed_event_received_on_message_bus(self, adapter, typed_bus):
        """UC-ADP-02: mapped event name delivers typed event on MessageBus."""
        h = CollectingHandler()
        typed_bus.subscribe(OrchestratorStartup, h)
        adapter.publish("orchestrator.startup", {"time": 1.0, "working_dir": "/tmp"})
        assert h.wait(timeout=1.0)
        assert len(h.received) == 1
        assert isinstance(h.received[0], OrchestratorStartup)
        assert h.received[0].time == 1.0

    def test_old_bus_still_receives(self, adapter, raw_event_bus):
        """UC-ADP-03: legacy subscribers on old bus still get events."""
        received = []
        raw_event_bus.subscribe("orchestrator.startup", lambda p: received.append(p))
        adapter.publish("orchestrator.startup", {"time": 1.0, "working_dir": "/tmp"})
        assert len(received) == 1
        assert received[0]["time"] == 1.0

    def test_field_mapping_camel_to_snake(self, adapter, typed_bus):
        """UC-ADP-04: camelCase payload keys mapped to snake_case fields."""
        h = CollectingHandler()
        typed_bus.subscribe(ToolInvoked, h)
        adapter.publish("tool.invoked", {
            "sessionUpdate": {"status": "ok"},
            "toolCallId": "tc1",
            "title": "test",
            "status": "invoked",
            "timestamp": 2.0,
            "workdir": "/repo",
        })
        assert h.wait(timeout=1.0)
        assert h.received[0].session_update == {"status": "ok"}
        assert h.received[0].tool_call_id == "tc1"

    def test_unmapped_event_still_on_old_bus(self, adapter, raw_event_bus):
        """UC-ADP-05: unmapped names only go to old bus (no crash)."""
        received = []
        raw_event_bus.subscribe("some.random.event", lambda p: received.append(p))
        adapter.publish("some.random.event", {"data": 42})
        assert received == [{"data": 42}]

    def test_non_dict_payload_does_not_crash(self, adapter, raw_event_bus):
        """UC-ADP-06: non-dict payload still delivered on old bus."""
        received = []
        raw_event_bus.subscribe("orchestrator.startup", lambda p: received.append(p))
        adapter.publish("orchestrator.startup", "bad_payload")
        assert received == ["bad_payload"]

    def test_none_payload_handled(self, adapter, raw_event_bus):
        """UC-ADP-07: None payload doesn't crash — old bus still receives."""
        received = []
        raw_event_bus.subscribe("provider.models.empty", lambda p: received.append(p))
        adapter.publish("provider.models.empty", None)
        assert len(received) == 1
        assert received[0] is None

    def test_correlation_id_filtered(self, adapter, typed_bus):
        """UC-ADP-08: _correlation_id from old payload excluded from typed event."""
        h = CollectingHandler()
        typed_bus.subscribe(GitBranch, h)
        adapter.publish("git.branch", {
            "branch": "main", "dirty": False, "ahead": 0, "behind": 0,
            "_correlation_id": "injected_by_old_bus",
        })
        assert h.wait(timeout=1.0)
        # The typed event's correlation_id should be auto-generated, not "injected_by_old_bus"
        assert h.received[0].correlation_id != "injected_by_old_bus"


# ---------------------------------------------------------------------------
# UC-ADP-09: publish_typed() reverse direction
# ---------------------------------------------------------------------------

class TestPublishTyped:
    """UC-ADP-09: publish_typed emits typed + old bus."""

    def test_typed_received_on_message_bus(self, adapter, typed_bus):
        h = CollectingHandler()
        typed_bus.subscribe(GitBranch, h)
        adapter.publish_typed(GitBranch(branch="feature", dirty=True, ahead=1, behind=0))
        assert h.wait(timeout=1.0)
        assert len(h.received) == 1
        assert h.received[0].branch == "feature"

    def test_dict_received_on_old_bus(self, adapter, raw_event_bus):
        received = []
        raw_event_bus.subscribe("git.branch", lambda p: received.append(p))
        adapter.publish_typed(GitBranch(branch="feature", dirty=True, ahead=1, behind=0))
        assert len(received) == 1
        assert received[0]["branch"] == "feature"


# ---------------------------------------------------------------------------
# UC-ADP-12: All delegate methods forwarded
# ---------------------------------------------------------------------------

class TestDelegateMethods:
    """UC-ADP-12: Non-publish methods forward to old bus."""

    def test_subscribe_to_agent(self, adapter, raw_event_bus):
        received = []
        cb = lambda msg: received.append(msg)
        adapter.subscribe_to_agent("agent_1", cb)
        raw_event_bus.publish_to_agent("agent_1", "hello")
        assert len(received) > 0

    def test_list_registered_agents(self, adapter, raw_event_bus):
        raw_event_bus.subscribe_to_agent("agent_a", lambda m: None)
        agents = adapter.list_registered_agents()
        assert "agent_a" in agents

    def test_publish_to_topic(self, adapter, raw_event_bus):
        received = []
        raw_event_bus.subscribe_to_topic("my_topic", lambda p: received.append(p))
        adapter.publish_to_topic("my_topic", {"data": 1}, sender_id="test")
        assert len(received) == 1
        assert received[0]["data"] == 1
        assert received[0]["sender_id"] == "test"
