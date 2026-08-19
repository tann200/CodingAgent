"""
Tests for EventBus -> MessageBus dual emission (formerly DualPublishBus).

Phase 5b: the DualPublishBus adapter has been removed; EventBus.publish()
now emits typed events on MessageBus directly.

Use-case table
==============
UC-ADP-01  EventBus with typed_bus emits typed event on MessageBus for mapped names
UC-ADP-02  EventBus.publish() still delivers via old subscribers (legacy unaffected)
UC-ADP-03  Field name mapper handles camelCase -> snake_case conversion
UC-ADP-04  Unmapped event names only delivered to old subscribers
UC-ADP-05  Bad payload (non-dict) doesn't crash -- old subscribers still receive
UC-ADP-06  None payload handled gracefully
UC-ADP-07  _correlation_id in payload filtered from typed event constructor
UC-ADP-08  EventBus.publish_typed() emits typed event on MessageBus
UC-ADP-09  get_typed_bus() returns a singleton MessageBus
UC-ADP-10  reset_typed_bus() clears singleton and shuts down the bus
UC-ADP-11  get_event_bus() returns EventBus with typed_bus attached
"""

import threading
import time

import pytest

from src.core.orchestration.event_bus import EventBus, get_event_bus
from src.core.messaging import (
    Event,
    GitBranch,
    MessageBus,
    OrchestratorStartup,
    ReliableEventAdmissionError,
    ToolInvoked,
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
    from src.core.orchestration.event_bus import reset_typed_bus
    reset_typed_bus()
    yield
    reset_typed_bus()


@pytest.fixture
def typed_bus() -> MessageBus:
    from src.core.orchestration.event_bus import get_typed_bus
    return get_typed_bus()


@pytest.fixture
def raw_event_bus() -> EventBus:
    """A plain EventBus (no MessageBus attached)."""
    return EventBus()


@pytest.fixture
def dual_bus(typed_bus) -> EventBus:
    """EventBus with MessageBus attached (as get_event_bus() returns)."""
    return EventBus(typed_bus=typed_bus)


# ---------------------------------------------------------------------------
# UC-ADP-01…07: publish() dual emission
# ---------------------------------------------------------------------------

class TestPublishDualEmission:
    """UC-ADP-01…07: EventBus.publish() sends to both buses when typed_bus set."""

    def test_typed_event_received_on_message_bus(self, dual_bus, typed_bus):
        """UC-ADP-01: mapped event name delivers typed event on MessageBus."""
        h = CollectingHandler()
        typed_bus.subscribe(OrchestratorStartup, h)
        dual_bus.publish("orchestrator.startup", {"time": 1.0, "working_dir": "/tmp"})
        assert h.wait(timeout=1.0)
        assert len(h.received) == 1
        assert isinstance(h.received[0], OrchestratorStartup)
        assert h.received[0].time == 1.0

    def test_old_subscribers_still_receive(self, dual_bus):
        """UC-ADP-02: legacy subscribers on same bus still get events."""
        received = []
        dual_bus.subscribe("orchestrator.startup", lambda p: received.append(p))
        dual_bus.publish("orchestrator.startup", {"time": 1.0, "working_dir": "/tmp"})
        assert len(received) == 1
        assert received[0]["time"] == 1.0

    def test_field_mapping_camel_to_snake(self, dual_bus, typed_bus):
        """UC-ADP-03: camelCase payload keys mapped to snake_case fields."""
        h = CollectingHandler()
        typed_bus.subscribe(ToolInvoked, h)
        dual_bus.publish("tool.invoked", {
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

    def test_unmapped_event_still_on_old_bus(self, dual_bus):
        """UC-ADP-04: unmapped names only go to old subscribers (no crash)."""
        received = []
        dual_bus.subscribe("some.random.event", lambda p: received.append(p))
        dual_bus.publish("some.random.event", {"data": 42})
        assert received == [{"data": 42}]

    def test_non_dict_payload_does_not_crash(self, dual_bus):
        """UC-ADP-05: non-dict payload still delivered to old subscribers."""
        received = []
        dual_bus.subscribe("orchestrator.startup", lambda p: received.append(p))
        dual_bus.publish("orchestrator.startup", "bad_payload")
        assert received == ["bad_payload"]

    def test_none_payload_handled(self, dual_bus):
        """UC-ADP-06: None payload doesn't crash."""
        received = []
        dual_bus.subscribe("provider.models.empty", lambda p: received.append(p))
        dual_bus.publish("provider.models.empty", None)
        assert len(received) == 1
        assert received[0] is None

    def test_correlation_id_filtered(self, dual_bus, typed_bus):
        """UC-ADP-07: _correlation_id excluded from typed event."""
        h = CollectingHandler()
        typed_bus.subscribe(GitBranch, h)
        dual_bus.publish("git.branch", {
            "branch": "main", "dirty": False, "ahead": 0, "behind": 0,
            "_correlation_id": "injected_by_old_bus",
        })
        assert h.wait(timeout=1.0)
        assert h.received[0].correlation_id != "injected_by_old_bus"

    def test_reliable_admission_failure_propagates(self):
        class RejectingTypedBus:
            def publish(self, event):
                raise ReliableEventAdmissionError("reliable lane full")

        bus = EventBus(typed_bus=RejectingTypedBus())
        with pytest.raises(ReliableEventAdmissionError):
            bus.publish(
                "orchestrator.startup",
                {"time": 1.0, "working_dir": "/tmp"},
            )


# ---------------------------------------------------------------------------
# UC-ADP-08: publish_typed() forward direction
# ---------------------------------------------------------------------------

class TestPublishTyped:
    """UC-ADP-08: EventBus.publish_typed emits on MessageBus."""

    def test_typed_received_on_message_bus(self, dual_bus, typed_bus):
        h = CollectingHandler()
        typed_bus.subscribe(GitBranch, h)
        dual_bus.publish_typed(GitBranch(branch="feature", dirty=True, ahead=1, behind=0))
        assert h.wait(timeout=1.0)
        assert len(h.received) == 1
        assert h.received[0].branch == "feature"

    def test_noop_without_typed_bus(self):
        """publish_typed does nothing when no MessageBus attached."""
        bus = EventBus()  # no typed_bus
        bus.publish_typed(GitBranch(branch="x", dirty=False, ahead=0, behind=0))
        # no crash, no event — correct


# ---------------------------------------------------------------------------
# UC-ADP-09…10: get_typed_bus / reset_typed_bus
# ---------------------------------------------------------------------------

class TestTypedBusSingleton:
    """UC-ADP-09…10: get_typed_bus / reset_typed_bus."""

    def test_get_typed_bus_returns_singleton(self):
        """UC-ADP-09: get_typed_bus() returns the same instance."""
        from src.core.orchestration.event_bus import get_typed_bus
        b1 = get_typed_bus()
        b2 = get_typed_bus()
        assert b1 is b2

    def test_reset_typed_bus_clears(self):
        """UC-ADP-10: reset_typed_bus() shuts down and clears."""
        from src.core.orchestration.event_bus import get_typed_bus, reset_typed_bus
        b1 = get_typed_bus()
        reset_typed_bus()
        b2 = get_typed_bus()
        assert b1 is not b2


# ---------------------------------------------------------------------------
# UC-ADP-11: get_event_bus() returns EventBus with typed_bus
# ---------------------------------------------------------------------------

class TestGetEventBus:
    """UC-ADP-11: get_event_bus() returns EventBus with typed_bus attached."""

    def test_get_event_bus_returns_event_bus(self):
        """get_event_bus() returns EventBus (not DualPublishBus)."""
        bus = get_event_bus()
        assert isinstance(bus, EventBus)
        assert bus._typed is not None
