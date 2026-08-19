"""
Base Event class for typed message bus.

All events in the system inherit from this base class, providing:
- Automatic correlation IDs for distributed tracing
- Automatic timestamps for observability
- Serialization support for logging/debugging
"""

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, ClassVar, Dict, Optional, Type


class EventDeliveryClass(str, Enum):
    """Admission and dispatch guarantee for a typed event."""

    TELEMETRY = "telemetry"
    ORDERED = "ordered"
    RELIABLE = "reliable"


@dataclass(frozen=True)
class EventDeliveryPolicy:
    """Resolved delivery policy for an event type."""

    delivery_class: EventDeliveryClass
    sequence_category: Optional[str] = None


# Only explicitly replaceable, high-volume updates are lossy. Everything else
# defaults to reliable so a newly-added approval or persistence event cannot
# silently inherit telemetry semantics.
_TELEMETRY_EVENT_TYPES = frozenset(
    {
        "AgentStatus",
        "PlanProgress",
        "ResponseStreamChunk",
        "ModelToken",
        "LLMToken",
        "TokenBudgetUpdate",
        "LogEntry",
    }
)

# Lifecycle events use reliable admission plus category-local FIFO dispatch.
# ToolInvoked is included because it persists invocation state.
_ORDERED_EVENT_CATEGORIES = {
    "ToolExecuteStart": "tool",
    "ToolInvoked": "tool",
    "ToolExecuteFinish": "tool",
    "ToolExecuteError": "tool",
    "StepStart": "step",
    "StepFinish": "step",
    "DelegationStart": "delegation",
    "DelegationFinish": "delegation",
}


def get_event_delivery_policy(event_type: Type["Event"]) -> EventDeliveryPolicy:
    """Return the centrally-enforced delivery policy for ``event_type``.

    Custom event classes may opt into telemetry or ordered delivery by defining
    ``delivery_class`` directly on the subclass. Production event types are
    classified here by name to avoid import cycles with ``event_types``.
    """
    explicit_class = event_type.__dict__.get("delivery_class")
    explicit_category = event_type.__dict__.get("sequence_category")
    if explicit_class is not None:
        delivery_class = EventDeliveryClass(explicit_class)
        if delivery_class is EventDeliveryClass.ORDERED and not explicit_category:
            raise ValueError(
                f"Ordered event {event_type.__name__} requires sequence_category"
            )
        return EventDeliveryPolicy(delivery_class, explicit_category)

    event_name = event_type.__name__
    sequence_category = _ORDERED_EVENT_CATEGORIES.get(event_name)
    if sequence_category is not None:
        return EventDeliveryPolicy(
            EventDeliveryClass.ORDERED,
            sequence_category=sequence_category,
        )
    if event_name in _TELEMETRY_EVENT_TYPES:
        return EventDeliveryPolicy(EventDeliveryClass.TELEMETRY)
    return EventDeliveryPolicy(EventDeliveryClass.RELIABLE)


@dataclass
class Event:
    """
    Base class for all typed events.
    
    Events are immutable data structures that represent things that have
    happened in the system. They are published to the MessageBus and
    delivered asynchronously to registered handlers.
    
    Attributes:
        correlation_id: Unique ID for tracing related events (auto-generated)
        timestamp: Unix timestamp when event was created (auto-set)
    
    Example:
        ```python
        @dataclass
        class UserLoggedIn(Event):
            user_id: str
            ip_address: str
        
        event = UserLoggedIn(user_id="alice", ip_address="192.168.1.1")
        message_bus.publish(event)
        ```
    """
    
    delivery_class: ClassVar[Optional[EventDeliveryClass]] = None
    sequence_category: ClassVar[Optional[str]] = None

    # Automatically set by MessageBus
    # kw_only=True allows subclasses to have required fields first
    correlation_id: str = field(
        default_factory=lambda: str(uuid.uuid4()), kw_only=True
    )
    timestamp: float = field(default_factory=time.time, kw_only=True)
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize event to dictionary for logging/debugging.
        
        Returns:
            Dictionary representation of event with all fields.
        
        Example:
            ```python
            event = UserLoggedIn(user_id="alice", ip_address="192.168.1.1")
            print(event.to_dict())
            # {
            #   "user_id": "alice",
            #   "ip_address": "192.168.1.1",
            #   "correlation_id": "uuid-...",
            #   "timestamp": 1234567890.123
            # }
            ```
        """
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        """
        Deserialize event from dictionary.
        
        Args:
            data: Dictionary with event fields
        
        Returns:
            Event instance
        
        Raises:
            TypeError: If required fields are missing
            ValueError: If field types don't match
        
        Example:
            ```python
            data = {
                "user_id": "alice",
                "ip_address": "192.168.1.1",
                "correlation_id": "uuid-...",
                "timestamp": 1234567890.123
            }
            event = UserLoggedIn.from_dict(data)
            ```
        """
        return cls(**data)
    
    def __str__(self) -> str:
        """Return human-readable event representation."""
        return f"{self.__class__.__name__}({self.to_dict()})"
