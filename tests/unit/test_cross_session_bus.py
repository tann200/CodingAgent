"""
Unit tests for cross_session_bus.py - Cross-Session P2P Bus
"""

import pytest
import time
from src.core.orchestration.cross_session_bus import (
    CrossSessionBus,
    CrossSessionMessage,
    MessagePriority,
    get_cross_session_bus,
)


@pytest.fixture(autouse=True)
def reset_bus():
    """Reset bus singleton before each test."""
    CrossSessionBus.reset_instance()
    yield
    CrossSessionBus.reset_instance()


class TestCrossSessionMessage:
    def test_initialization(self):
        msg = CrossSessionMessage(
            message_id="msg-123",
            topic="agent.broadcast",
            sender_session_id="session-1",
            sender_role="operational",
            payload={"key": "value"},
            priority=MessagePriority.NORMAL,
            timestamp=time.time(),
        )
        assert msg.message_id == "msg-123"
        assert msg.topic == "agent.broadcast"
        assert msg.payload == {"key": "value"}

    def test_is_expired_false(self):
        msg = CrossSessionMessage(
            message_id="msg-123",
            topic="agent.broadcast",
            sender_session_id="session-1",
            sender_role="operational",
            payload={},
            priority=MessagePriority.NORMAL,
            timestamp=time.time(),
            ttl=300.0,
        )
        assert msg.is_expired is False

    def test_is_expired_true(self):
        msg = CrossSessionMessage(
            message_id="msg-123",
            topic="agent.broadcast",
            sender_session_id="session-1",
            sender_role="operational",
            payload={},
            priority=MessagePriority.NORMAL,
            timestamp=time.time() - 400,
            ttl=300.0,
        )
        assert msg.is_expired is True


class TestCrossSessionBus:
    def test_singleton(self):
        bus1 = CrossSessionBus.get_instance()
        bus2 = CrossSessionBus.get_instance()
        assert bus1 is bus2

    def test_subscribe(self):
        bus = CrossSessionBus.get_instance()
        received = []

        def handler(msg):
            received.append(msg)

        sub_id = bus.subscribe("test.topic", handler)
        assert sub_id is not None

    def test_unsubscribe(self):
        bus = CrossSessionBus.get_instance()

        def handler(msg):
            pass

        sub_id = bus.subscribe("test.topic", handler)
        result = bus.unsubscribe(sub_id)
        assert result is True

    def test_unsubscribe_not_found(self):
        bus = CrossSessionBus.get_instance()
        result = bus.unsubscribe("nonexistent-id")
        assert result is False

    def test_unsubscribe_session(self):
        bus = CrossSessionBus.get_instance()

        def handler1(msg):
            pass

        def handler2(msg):
            pass

        bus.subscribe("test.topic", handler1, session_id="session-1")
        bus.subscribe("test.topic", handler2, session_id="session-2")
        removed = bus.unsubscribe_session("session-1")
        assert removed == 1

    def test_publish_delivers_to_subscriber(self):
        bus = CrossSessionBus.get_instance()
        received = []

        def handler(msg):
            received.append(msg)

        bus.subscribe("test.topic", handler)
        bus.publish(
            topic="test.topic",
            sender_session_id="session-1",
            sender_role="operational",
            payload={"data": "test"},
        )
        assert len(received) == 1
        assert received[0].payload["data"] == "test"

    def test_publish_does_not_deliver_to_other_topic(self):
        bus = CrossSessionBus.get_instance()
        received = []

        def handler(msg):
            received.append(msg)

        bus.subscribe("test.topic", handler)
        bus.publish(
            topic="other.topic",
            sender_session_id="session-1",
            sender_role="operational",
            payload={"data": "test"},
        )
        assert len(received) == 0

    def test_publish_with_wildcard_subscription(self):
        bus = CrossSessionBus.get_instance()
        received = []

        def handler(msg):
            received.append(msg)

        bus.subscribe("agent.*", handler)
        bus.publish(
            topic="agent.broadcast",
            sender_session_id="session-1",
            sender_role="operational",
            payload={"data": "test"},
        )
        assert len(received) == 1

    def test_publish_with_session_filter(self):
        bus = CrossSessionBus.get_instance()
        received = []

        def handler(msg):
            received.append(msg)

        bus.subscribe("test.topic", handler, session_id="session-2")
        bus.publish(
            topic="test.topic",
            sender_session_id="session-1",
            sender_role="operational",
            payload={"data": "test"},
        )
        assert len(received) == 0

    def test_publish_stores_in_history(self):
        bus = CrossSessionBus.get_instance()

        def handler(msg):
            pass

        bus.subscribe("test.topic", handler)
        bus.publish(
            topic="test.topic",
            sender_session_id="session-1",
            sender_role="operational",
            payload={"data": "test"},
        )
        messages = bus.get_messages()
        assert len(messages) == 1
        assert messages[0].payload["data"] == "test"

    def test_send_reply(self):
        bus = CrossSessionBus.get_instance()
        original = CrossSessionMessage(
            message_id="msg-123",
            topic="test.topic",
            sender_session_id="session-1",
            sender_role="operational",
            payload={},
            priority=MessagePriority.NORMAL,
            timestamp=time.time(),
        )
        received = []

        def handler(msg):
            received.append(msg)

        bus.subscribe("test.topic", handler)
        bus.send_reply(
            original_message=original,
            payload={"reply": "data"},
            sender_session_id="session-2",
            sender_role="analyst",
        )
        assert len(received) == 1
        assert received[0].reply_to == "msg-123"

    def test_get_messages_with_topic_filter(self):
        bus = CrossSessionBus.get_instance()

        def handler(msg):
            pass

        bus.subscribe("test.topic", handler)
        bus.publish(
            topic="test.topic", sender_session_id="s1", sender_role="r", payload={}
        )
        bus.publish(
            topic="other.topic", sender_session_id="s1", sender_role="r", payload={}
        )
        messages = bus.get_messages(topic="test.topic")
        assert len(messages) == 1

    def test_get_messages_with_sender_filter(self):
        bus = CrossSessionBus.get_instance()

        def handler(msg):
            pass

        bus.subscribe("test.topic", handler)
        bus.publish(
            topic="test.topic", sender_session_id="s1", sender_role="r", payload={}
        )
        bus.publish(
            topic="test.topic", sender_session_id="s2", sender_role="r", payload={}
        )
        messages = bus.get_messages(sender_session_id="s1")
        assert len(messages) == 1

    def test_get_messages_with_since_filter(self):
        bus = CrossSessionBus.get_instance()

        def handler(msg):
            pass

        bus.subscribe("test.topic", handler)
        bus.publish(
            topic="test.topic", sender_session_id="s1", sender_role="r", payload={}
        )
        since = time.time()
        bus.publish(
            topic="test.topic", sender_session_id="s1", sender_role="r", payload={}
        )
        messages = bus.get_messages(since=since)
        assert len(messages) == 1

    def test_get_messages_with_limit(self):
        bus = CrossSessionBus.get_instance()

        def handler(msg):
            pass

        bus.subscribe("test.topic", handler)
        for i in range(10):
            bus.publish(
                topic="test.topic",
                sender_session_id="s1",
                sender_role="r",
                payload={"i": i},
            )
        messages = bus.get_messages(limit=5)
        assert len(messages) == 5

    def test_get_statistics(self):
        bus = CrossSessionBus.get_instance()

        def handler1(msg):
            pass

        def handler2(msg):
            pass

        bus.subscribe("topic1", handler1)
        bus.subscribe("topic2", handler2)
        bus.publish(topic="topic1", sender_session_id="s1", sender_role="r", payload={})
        bus.publish(topic="topic1", sender_session_id="s1", sender_role="r", payload={})

        stats = bus.get_statistics()
        assert stats["total_subscriptions"] == 2
        assert stats["message_history_size"] == 2

    def test_cleanup_expired(self):
        bus = CrossSessionBus.get_instance()

        def handler(msg):
            pass

        bus.subscribe("test.topic", handler)
        # Create old message
        old_msg = CrossSessionMessage(
            message_id="old-msg",
            topic="test.topic",
            sender_session_id="s1",
            sender_role="r",
            payload={},
            priority=MessagePriority.NORMAL,
            timestamp=time.time() - 400,
            ttl=300.0,
        )
        bus._message_history.append(old_msg)
        # Create new message
        bus.publish(
            topic="test.topic", sender_session_id="s1", sender_role="r", payload={}
        )

        removed = bus.cleanup_expired()
        assert removed == 1
        assert len(bus._message_history) == 1

    def test_topic_matches_exact(self):
        bus = CrossSessionBus.get_instance()
        assert bus._topic_matches("test.topic", "test.topic") is True
        assert bus._topic_matches("test.topic", "other.topic") is False

    def test_topic_matches_wildcard_prefix(self):
        bus = CrossSessionBus.get_instance()
        assert bus._topic_matches("test.topic", "test.*") is True
        assert bus._topic_matches("test.other", "test.*") is True
        assert bus._topic_matches("other.topic", "test.*") is False

    def test_topic_matches_wildcard_suffix(self):
        bus = CrossSessionBus.get_instance()
        # *.broadcast matches anything ending with .broadcast
        assert bus._topic_matches("agent.broadcast", "*.broadcast") is True
        assert bus._topic_matches("other.broadcast", "*.broadcast") is True
        assert bus._topic_matches("foo.bar.broadcast", "*.broadcast") is True
        assert bus._topic_matches("broadcast", "*.broadcast") is False  # No dot prefix

    def test_topic_matches_any(self):
        bus = CrossSessionBus.get_instance()
        assert bus._topic_matches("anything.here", "*") is True


def test_get_cross_session_bus():
    """Test module-level getter."""
    CrossSessionBus.reset_instance()
    bus = get_cross_session_bus()
    assert bus is not None
    CrossSessionBus.reset_instance()


@pytest.mark.asyncio
async def test_send_and_wait_uses_running_loop():
    """Regression: send_and_wait must use get_running_loop() not get_event_loop().

    Before the fix, asyncio.get_event_loop().create_future() raised DeprecationWarning
    or RuntimeError in Python 3.10+ when called inside an async context. The fix uses
    asyncio.get_running_loop() which is always correct when already in async code.
    """
    import asyncio
    CrossSessionBus.reset_instance()
    bus = CrossSessionBus.get_instance()

    # Register a subscriber that immediately replies
    async def _reply_after_publish():
        await asyncio.sleep(0.01)
        # Simulate a reply by directly resolving the pending future
        with bus._lock:
            for cid, fut in list(bus._pending_replies.items()):
                if not fut.done():
                    # Create a mock reply message
                    reply = CrossSessionMessage(
                        message_id="reply-1",
                        topic="test.reply",
                        sender_session_id="responder",
                        sender_role="tester",
                        payload={"ack": True},
                        correlation_id=cid,
                    )
                    fut.get_loop().call_soon_threadsafe(fut.set_result, reply)

    asyncio.create_task(_reply_after_publish())

    result = await bus.send_and_wait(
        topic="test.request",
        sender_session_id="requester",
        sender_role="tester",
        payload={"ping": True},
        timeout=0.5,
    )
    # Either we get a result or None (timeout) — but no RuntimeError or DeprecationWarning
    assert result is None or isinstance(result, CrossSessionMessage)
    CrossSessionBus.reset_instance()
