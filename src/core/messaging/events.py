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
from typing import Any, Dict


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
