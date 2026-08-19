"""
MessageBus metrics collection for observability.

Tracks:
- Events published/delivered/dropped
- Handler failures
- Delivery latency (p50, p99)
"""

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class MessageBusMetrics:
    """
    Metrics for message bus observability.
    
    Tracks event publishing, delivery, drops, handler failures, and latency.
    All metrics are in-memory and reset on restart.
    
    Attributes:
        published: Count of events published by type
        delivered: Count of events successfully delivered by type
        dropped: Count of events dropped (queue full) by type
        handler_failed: Count of handler failures by event:handler
        delivery_duration_ms: Delivery latency samples by event:handler
    
    Example:
        ```python
        metrics = MessageBusMetrics()
        metrics.increment_published("AgentStarted")
        metrics.record_delivery("AgentStarted", "TUIEventHandler", 12.5)
        
        snapshot = metrics.snapshot()
        print(f"Published: {snapshot['published']}")
        print(f"P99 latency: {snapshot['p99_delivery_ms']}")
        ```
    """
    
    published: Counter = field(default_factory=Counter)
    delivered: Counter = field(default_factory=Counter)
    dropped: Counter = field(default_factory=Counter)
    handler_failed: Counter = field(default_factory=Counter)
    delivery_duration_ms: Dict[str, List[float]] = field(default_factory=dict)
    
    def increment_published(self, event_type: str) -> None:
        """
        Increment published count for event type.
        
        Args:
            event_type: Name of event class (e.g., "AgentStarted")
        """
        self.published[event_type] += 1
    
    def increment_dropped(self, event_type: str) -> None:
        """
        Increment dropped count for event type.
        
        Called when queue is full and event cannot be queued.
        
        Args:
            event_type: Name of event class
        """
        self.dropped[event_type] += 1
    
    def increment_handler_failed(self, event_type: str, handler: str) -> None:
        """
        Increment handler failure count.
        
        Called when handler raises exception during event processing.
        
        Args:
            event_type: Name of event class
            handler: Name of handler class
        """
        key = f"{event_type}:{handler}"
        self.handler_failed[key] += 1
    
    def record_delivery(
        self, event_type: str, handler: str, duration_ms: float
    ) -> None:
        """
        Record successful event delivery with latency.
        
        Args:
            event_type: Name of event class
            handler: Name of handler class
            duration_ms: Time taken to deliver event (milliseconds)
        """
        self.delivered[event_type] += 1
        key = f"{event_type}:{handler}"
        self.delivery_duration_ms.setdefault(key, []).append(duration_ms)
    
    def snapshot(self) -> Dict[str, Any]:
        """
        Return metrics snapshot for observability.
        
        Returns:
            Dictionary with all metrics including computed percentiles.
        
        Example:
            ```python
            snapshot = metrics.snapshot()
            # {
            #   "published": {"AgentStarted": 100, "ToolCallStarted": 50},
            #   "delivered": {"AgentStarted": 100, "ToolCallStarted": 49},
            #   "dropped": {"ToolCallStarted": 1},
            #   "handler_failed": {},
            #   "p50_delivery_ms": {"AgentStarted:TUIHandler": 10.5},
            #   "p99_delivery_ms": {"AgentStarted:TUIHandler": 25.3}
            # }
            ```
        """
        return {
            "published": dict(self.published),
            "delivered": dict(self.delivered),
            "dropped": dict(self.dropped),
            "handler_failed": dict(self.handler_failed),
            "p50_delivery_ms": {
                k: statistics.median(v)
                for k, v in self.delivery_duration_ms.items()
                if v
            },
            "p99_delivery_ms": {
                k: (
                    statistics.quantiles(v, n=100)[98]
                    if len(v) >= 100
                    else max(v)
                )
                for k, v in self.delivery_duration_ms.items()
                if v
            },
        }
    
    def reset(self) -> None:
        """Clear all metrics (useful for testing)."""
        self.published.clear()
        self.delivered.clear()
        self.dropped.clear()
        self.handler_failed.clear()
        self.delivery_duration_ms.clear()
