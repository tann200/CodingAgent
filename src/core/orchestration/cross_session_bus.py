"""
CrossSessionBus - P2P communication between sessions.

Provides:
- Inter-session messaging (not just within a session)
- Topic-based pub/sub across sessions
- Session-aware message filtering
- Message persistence and replay
"""

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Callable, Any, Union
from pathlib import Path
import json

logger = logging.getLogger(__name__)


class MessagePriority(Enum):
    """Message priority levels."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class CrossSessionMessage:
    """A message sent between sessions."""

    message_id: str
    topic: str
    sender_session_id: str
    sender_role: str
    payload: Dict[str, Any]
    priority: MessagePriority
    timestamp: float
    reply_to: Optional[str] = None
    correlation_id: Optional[str] = None
    ttl: float = 300.0  # 5 minutes default TTL
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Check if message has expired."""
        return (time.time() - self.timestamp) > self.ttl


@dataclass
class Subscription:
    """A subscription to a topic."""

    subscription_id: str
    session_id: str
    topic_pattern: str  # Supports wildcards: "agent.*", "session.*"
    callback: Callable[[CrossSessionMessage], None]
    created_at: float


class CrossSessionBus:
    """
    P2P message bus for cross-session communication.

    Extends EventBus with session-awareness:
    - Messages are tagged with sender session
    - Subscriptions can filter by sender session
    - Messages can be persisted for replay
    - Priority-based delivery

    Usage:
        bus = CrossSessionBus.get_instance()

        # Subscribe to messages
        def handle_files(msg):
            print(f"Files from {msg.sender_session_id}: {msg.payload}")

        bus.subscribe("agent.scout.broadcast", handle_files, session_id="main_session")

        # Send message
        bus.publish(
            topic="agent.scout.broadcast",
            sender_session_id="scout_123",
            sender_role="scout",
            payload={"files": ["auth.py", "login.py"]}
        )
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        # Subscriptions by topic pattern
        self._subscriptions: Dict[str, List[Subscription]] = {}

        # Message history for replay (bounded)
        self._message_history: List[CrossSessionMessage] = []
        self._max_history = 1000

        # Pending replies (for request-response pattern)
        self._pending_replies: Dict[str, asyncio.Future] = {}
        self._reply_timeout = 30.0

        # Lock for thread safety
        self._lock = threading.RLock()

        # Async support
        self._async_mode = False

        # Persistence
        self._persistence_dir: Optional[Path] = None

    @classmethod
    def get_instance(cls) -> "CrossSessionBus":
        """Get the singleton instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing)."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.shutdown()
            cls._instance = None

    def set_persistence(self, persistence_dir: Union[str, Path]) -> None:
        """
        Enable message persistence to disk.

        Args:
            persistence_dir: Directory for message persistence
        """
        self._persistence_dir = Path(persistence_dir)
        self._persistence_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"CrossSessionBus persistence enabled: {self._persistence_dir}")

    def subscribe(
        self,
        topic: str,
        callback: Callable[[CrossSessionMessage], None],
        session_id: Optional[str] = None,
        subscription_id: Optional[str] = None,
    ) -> str:
        """
        Subscribe to a topic.

        Args:
            topic: Topic pattern (supports wildcards like "agent.*")
            callback: Function to call when message received
            session_id: Optional session filter (only receive from this session)
            subscription_id: Optional explicit subscription ID

        Returns:
            Subscription ID
        """
        with self._lock:
            if subscription_id is None:
                subscription_id = str(uuid.uuid4())[:8]

            sub = Subscription(
                subscription_id=subscription_id,
                session_id=session_id or "",
                topic_pattern=topic,
                callback=callback,
                created_at=time.time(),
            )

            if topic not in self._subscriptions:
                self._subscriptions[topic] = []
            self._subscriptions[topic].append(sub)

            logger.debug(
                f"Subscribed to {topic} (session={session_id}, sub_id={subscription_id})"
            )

            return subscription_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """
        Unsubscribe from a topic.

        Args:
            subscription_id: Subscription to remove

        Returns:
            True if unsubscribed, False if not found
        """
        with self._lock:
            for topic, subs in self._subscriptions.items():
                for i, sub in enumerate(subs):
                    if sub.subscription_id == subscription_id:
                        subs.pop(i)
                        logger.debug(f"Unsubscribed from {topic} ({subscription_id})")
                        return True
            return False

    def unsubscribe_session(self, session_id: str) -> int:
        """
        Unsubscribe all callbacks for a session.

        Args:
            session_id: Session to unsubscribe

        Returns:
            Number of subscriptions removed
        """
        with self._lock:
            removed = 0
            for topic in list(self._subscriptions.keys()):
                subs = self._subscriptions[topic]
                self._subscriptions[topic] = [
                    s for s in subs if s.session_id != session_id
                ]
                removed += len(subs) - len(self._subscriptions[topic])

            if removed > 0:
                logger.debug(
                    f"Unsubscribed {removed} subscriptions for session {session_id}"
                )

            return removed

    def publish(
        self,
        topic: str,
        sender_session_id: str,
        sender_role: str,
        payload: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL,
        reply_to: Optional[str] = None,
        correlation_id: Optional[str] = None,
        ttl: float = 300.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Publish a message to a topic.

        Args:
            topic: Topic to publish to
            sender_session_id: Session sending the message
            sender_role: Role of the sender
            payload: Message payload
            priority: Message priority
            reply_to: Optional message ID to reply to
            correlation_id: Optional correlation ID for tracing
            ttl: Time-to-live in seconds
            metadata: Additional metadata

        Returns:
            Message ID
        """
        message_id = str(uuid.uuid4())[:12]

        message = CrossSessionMessage(
            message_id=message_id,
            topic=topic,
            sender_session_id=sender_session_id,
            sender_role=sender_role,
            payload=payload,
            priority=priority,
            timestamp=time.time(),
            reply_to=reply_to,
            correlation_id=correlation_id,
            ttl=ttl,
            metadata=metadata or {},
        )

        # Store in history
        with self._lock:
            self._message_history.append(message)
            if len(self._message_history) > self._max_history:
                self._message_history = self._message_history[-self._max_history :]

        # Persist if enabled
        if self._persistence_dir:
            self._persist_message(message)

        # Deliver to subscribers
        self._deliver_message(message)

        logger.debug(
            f"Published to {topic} from {sender_session_id} "
            f"(msg_id={message_id}, priority={priority.name})"
        )

        return message_id

    def send_reply(
        self,
        original_message: CrossSessionMessage,
        payload: Dict[str, Any],
        sender_session_id: str,
        sender_role: str,
    ) -> str:
        """
        Send a reply to a message.

        Args:
            original_message: Message being replied to
            payload: Reply payload
            sender_session_id: Session sending the reply
            sender_role: Role sending the reply

        Returns:
            Message ID of the reply
        """
        return self.publish(
            topic=original_message.topic,
            sender_session_id=sender_session_id,
            sender_role=sender_role,
            payload=payload,
            reply_to=original_message.message_id,
            correlation_id=original_message.correlation_id,
        )

    async def send_and_wait(
        self,
        topic: str,
        sender_session_id: str,
        sender_role: str,
        payload: Dict[str, Any],
        timeout: float = 30.0,
    ) -> Optional[CrossSessionMessage]:
        """
        Send a message and wait for a reply.

        Args:
            topic: Topic to send to
            sender_session_id: Session sending
            sender_role: Role sending
            payload: Message payload
            timeout: Timeout in seconds

        Returns:
            Reply message or None if timeout
        """
        correlation_id = str(uuid.uuid4())[:8]

        # Create future for reply
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        with self._lock:
            self._pending_replies[correlation_id] = future

        # Send message
        self.publish(
            topic=topic,
            sender_session_id=sender_session_id,
            sender_role=sender_role,
            payload=payload,
            correlation_id=correlation_id,
        )

        # Wait for reply with timeout
        try:
            reply = await asyncio.wait_for(future, timeout=timeout)
            return reply
        except asyncio.TimeoutError:
            logger.warning(
                f"send_and_wait timed out after {timeout}s (correlation={correlation_id})"
            )
            return None
        finally:
            # Cancel the future before removing it so any other awaiter gets a clean
            # CancelledError instead of hanging indefinitely on a leaked future.
            if not future.done():
                future.cancel()
            with self._lock:
                self._pending_replies.pop(correlation_id, None)

    def get_messages(
        self,
        topic: Optional[str] = None,
        sender_session_id: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> List[CrossSessionMessage]:
        """
        Get messages from history.

        Args:
            topic: Filter by topic pattern
            sender_session_id: Filter by sender
            since: Only messages after this timestamp
            limit: Maximum messages to return

        Returns:
            List of matching messages
        """
        with self._lock:
            messages = self._message_history.copy()

        # Apply filters
        if topic:
            messages = [m for m in messages if self._topic_matches(m.topic, topic)]

        if sender_session_id:
            messages = [m for m in messages if m.sender_session_id == sender_session_id]

        if since:
            messages = [m for m in messages if m.timestamp >= since]

        # Sort by timestamp descending, limit
        messages.sort(key=lambda m: m.timestamp, reverse=True)
        return messages[:limit]

    def get_statistics(self) -> Dict[str, Any]:
        """Get bus statistics."""
        with self._lock:
            topic_counts: Dict[str, int] = {}
            for topic, subs in self._subscriptions.items():
                topic_counts[topic] = len(subs)

            return {
                "total_subscriptions": sum(
                    len(s) for s in self._subscriptions.values()
                ),
                "subscriptions_by_topic": topic_counts,
                "message_history_size": len(self._message_history),
                "pending_replies": len(self._pending_replies),
                "persistence_enabled": self._persistence_dir is not None,
            }

    def _deliver_message(self, message: CrossSessionMessage) -> None:
        """Deliver message to matching subscribers."""
        with self._lock:
            for topic, subs in list(self._subscriptions.items()):
                if self._topic_matches(message.topic, topic):
                    for sub in subs:
                        # Check session filter
                        if (
                            sub.session_id
                            and sub.session_id != message.sender_session_id
                        ):
                            continue

                        # Deliver message (handle async in separate task)
                        try:
                            if asyncio.iscoroutinefunction(sub.callback):
                                # asyncio.create_task() requires a running event loop.
                                # Guard against calls from non-async contexts (e.g. sync
                                # threads) by checking for a running loop first.
                                try:
                                    loop = asyncio.get_running_loop()
                                    loop.create_task(self._deliver_async(sub, message))
                                except RuntimeError:
                                    logger.warning(
                                        f"CrossSessionBus: no running event loop for async "
                                        f"delivery to {sub.subscription_id}; skipping"
                                    )
                            else:
                                sub.callback(message)
                        except Exception as e:
                            logger.error(
                                f"Error delivering to subscription {sub.subscription_id}: {e}"
                            )

    async def _deliver_async(
        self, sub: Subscription, message: CrossSessionMessage
    ) -> None:
        """Deliver message to async callback."""
        try:
            await sub.callback(message)
        except Exception as e:
            logger.error(f"Error in async delivery to {sub.subscription_id}: {e}")

    def _topic_matches(self, message_topic: str, pattern: str) -> bool:
        """Check if topic matches pattern (supports wildcards)."""
        if pattern == "*":
            return True
        if pattern == message_topic:
            return True

        # Handle wildcards
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return message_topic.startswith(prefix + ".")
        if pattern.startswith("*."):
            suffix = pattern[2:]
            return message_topic.endswith("." + suffix)

        return False

    def _persist_message(self, message: CrossSessionMessage) -> None:
        """Persist message to disk."""
        if not self._persistence_dir:
            return

        try:
            msg_file = self._persistence_dir / f"msg_{message.message_id}.json"
            msg_data = {
                "message_id": message.message_id,
                "topic": message.topic,
                "sender_session_id": message.sender_session_id,
                "sender_role": message.sender_role,
                "payload": message.payload,
                "priority": message.priority.value,
                "timestamp": message.timestamp,
                "reply_to": message.reply_to,
                "correlation_id": message.correlation_id,
                "ttl": message.ttl,
                "metadata": message.metadata,
            }
            msg_file.write_text(json.dumps(msg_data))
        except Exception as e:
            logger.error(f"Failed to persist message: {e}")

    def cleanup_expired(self) -> int:
        """
        Remove expired messages from history.

        Returns:
            Number of messages removed
        """
        with self._lock:
            before = len(self._message_history)
            self._message_history = [
                m for m in self._message_history if not m.is_expired
            ]
            removed = before - len(self._message_history)

            if removed > 0:
                logger.debug(f"Cleaned up {removed} expired messages")

            return removed

    def shutdown(self) -> None:
        """Shutdown the bus."""
        with self._lock:
            self._subscriptions.clear()
            self._message_history.clear()
            self._pending_replies.clear()

        logger.info("CrossSessionBus shutdown complete")


def get_cross_session_bus() -> CrossSessionBus:
    """Get the global cross-session bus instance."""
    return CrossSessionBus.get_instance()
