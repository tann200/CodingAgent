"""Tests for correlation ID support in EventBus (#26).

Verifies:
- new_correlation_id() generates a UUID and sets it in context
- get_correlation_id() / set_correlation_id() round-trip
- EventBus.publish() injects _correlation_id into dict payloads
- Explicit correlation_id kwarg overrides context var
- Non-dict payloads are left untouched
- Payloads already containing _correlation_id are not overwritten
"""

import uuid
import pytest

from src.core.orchestration.event_bus import (
    EventBus,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
    _current_correlation_id,
)


def _reset_cid():
    """Helper: clear the context var between tests."""
    _current_correlation_id.set(None)


class TestCorrelationIdHelpers:
    def setup_method(self):
        _reset_cid()

    def test_new_correlation_id_returns_valid_uuid(self):
        cid = new_correlation_id()
        uuid.UUID(cid)  # raises ValueError if not a valid UUID

    def test_new_correlation_id_sets_context(self):
        cid = new_correlation_id()
        assert get_correlation_id() == cid

    def test_set_and_get_round_trip(self):
        set_correlation_id("abc-123")
        assert get_correlation_id() == "abc-123"

    def test_get_returns_none_before_set(self):
        assert get_correlation_id() is None

    def test_successive_new_calls_replace_context(self):
        cid1 = new_correlation_id()
        cid2 = new_correlation_id()
        assert cid1 != cid2
        assert get_correlation_id() == cid2


class TestEventBusCorrelationInjection:
    def setup_method(self):
        _reset_cid()

    def test_publish_injects_correlation_id_into_dict_payload(self):
        bus = EventBus()
        received = []
        bus.subscribe("test.event", received.append)

        set_correlation_id("test-cid-001")
        bus.publish("test.event", {"tool": "read_file"})

        assert len(received) == 1
        assert received[0]["_correlation_id"] == "test-cid-001"
        assert received[0]["tool"] == "read_file"  # original keys preserved

    def test_publish_explicit_correlation_id_overrides_context(self):
        bus = EventBus()
        received = []
        bus.subscribe("test.event", received.append)

        set_correlation_id("context-cid")
        bus.publish("test.event", {"x": 1}, correlation_id="explicit-cid")

        assert received[0]["_correlation_id"] == "explicit-cid"

    def test_publish_no_cid_leaves_dict_unchanged(self):
        bus = EventBus()
        received = []
        bus.subscribe("test.event", received.append)

        # No correlation_id set in context
        bus.publish("test.event", {"x": 1})

        assert "_correlation_id" not in received[0]

    def test_publish_does_not_overwrite_existing_correlation_id(self):
        bus = EventBus()
        received = []
        bus.subscribe("test.event", received.append)

        set_correlation_id("new-cid")
        bus.publish("test.event", {"_correlation_id": "original-cid", "x": 1})

        assert received[0]["_correlation_id"] == "original-cid"

    def test_publish_non_dict_payload_untouched(self):
        bus = EventBus()
        received = []
        bus.subscribe("test.event", received.append)

        set_correlation_id("some-cid")
        bus.publish("test.event", "plain string payload")

        assert received[0] == "plain string payload"

    def test_publish_list_payload_untouched(self):
        bus = EventBus()
        received = []
        bus.subscribe("test.event", received.append)

        set_correlation_id("some-cid")
        bus.publish("test.event", [1, 2, 3])

        assert received[0] == [1, 2, 3]

    def test_original_payload_dict_not_mutated(self):
        """publish() must copy the dict, not mutate the caller's dict in-place."""
        bus = EventBus()
        bus.subscribe("test.event", lambda _: None)

        set_correlation_id("cid-x")
        payload = {"tool": "write_file"}
        bus.publish("test.event", payload)

        assert "_correlation_id" not in payload

    def test_correlation_id_propagates_across_multiple_events(self):
        bus = EventBus()
        received = []
        bus.subscribe("a", received.append)
        bus.subscribe("b", received.append)

        cid = new_correlation_id()
        bus.publish("a", {"step": 1})
        bus.publish("b", {"step": 2})

        assert received[0]["_correlation_id"] == cid
        assert received[1]["_correlation_id"] == cid
