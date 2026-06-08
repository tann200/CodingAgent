"""Tests for Event base class."""

import time
from dataclasses import dataclass

import pytest

from src.core.messaging.events import Event


@dataclass
class TestEvent(Event):
    """Test event with custom fields."""
    
    message: str
    count: int


def test_event_auto_generates_correlation_id():
    """Verify correlation_id is auto-generated."""
    event = TestEvent(message="hello", count=1)
    assert event.correlation_id is not None
    assert len(event.correlation_id) > 0


def test_event_auto_sets_timestamp():
    """Verify timestamp is auto-set to current time."""
    before = time.time()
    event = TestEvent(message="hello", count=1)
    after = time.time()
    
    assert before <= event.timestamp <= after


def test_event_correlation_ids_are_unique():
    """Verify each event gets unique correlation_id."""
    event1 = TestEvent(message="one", count=1)
    event2 = TestEvent(message="two", count=2)
    
    assert event1.correlation_id != event2.correlation_id


def test_event_can_override_correlation_id():
    """Verify correlation_id can be explicitly set."""
    custom_id = "custom-correlation-id"
    event = TestEvent(
        message="hello",
        count=1,
        correlation_id=custom_id
    )
    
    assert event.correlation_id == custom_id


def test_event_can_override_timestamp():
    """Verify timestamp can be explicitly set."""
    custom_timestamp = 1234567890.123
    event = TestEvent(
        message="hello",
        count=1,
        timestamp=custom_timestamp
    )
    
    assert event.timestamp == custom_timestamp


def test_event_to_dict():
    """Verify to_dict() returns all fields."""
    event = TestEvent(message="hello", count=42)
    data = event.to_dict()
    
    assert data["message"] == "hello"
    assert data["count"] == 42
    assert "correlation_id" in data
    assert "timestamp" in data


def test_event_from_dict():
    """Verify from_dict() deserializes correctly."""
    data = {
        "message": "hello",
        "count": 42,
        "correlation_id": "test-id",
        "timestamp": 1234567890.123,
    }
    
    event = TestEvent.from_dict(data)
    
    assert event.message == "hello"
    assert event.count == 42
    assert event.correlation_id == "test-id"
    assert event.timestamp == 1234567890.123


def test_event_round_trip_serialization():
    """Verify to_dict() and from_dict() round-trip correctly."""
    original = TestEvent(message="test", count=99)
    data = original.to_dict()
    restored = TestEvent.from_dict(data)
    
    assert restored.message == original.message
    assert restored.count == original.count
    assert restored.correlation_id == original.correlation_id
    assert restored.timestamp == original.timestamp


def test_event_from_dict_missing_fields():
    """Verify from_dict() raises error for missing required fields."""
    data = {"message": "hello"}  # Missing 'count'
    
    with pytest.raises(TypeError):
        TestEvent.from_dict(data)


def test_event_str_representation():
    """Verify __str__() returns readable representation."""
    event = TestEvent(message="hello", count=1)
    event_str = str(event)
    
    assert "TestEvent" in event_str
    assert "hello" in event_str
    assert "1" in event_str
