"""
MessageBus metrics collection for observability.

Tracks:
- Events published/delivered/dropped
- Handler failures
- Delivery latency (p50, p99)
"""

import statistics
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional


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

    max_latency_samples: int = 1024
    published: Counter = field(default_factory=Counter)
    delivered: Counter = field(default_factory=Counter)
    dropped: Counter = field(default_factory=Counter)
    handler_failed: Counter = field(default_factory=Counter)
    admitted_by_class: Counter = field(default_factory=Counter)
    admission_failed_by_class: Counter = field(default_factory=Counter)
    delivered_by_class: Counter = field(default_factory=Counter)
    dropped_by_class: Counter = field(default_factory=Counter)
    queue_depth: Dict[str, int] = field(default_factory=dict)
    queue_depth_high_water: Dict[str, int] = field(default_factory=dict)
    delivery_duration_ms: Dict[str, Deque[float]] = field(default_factory=dict)
    delivery_latency_ms_by_class: Dict[str, Deque[float]] = field(
        default_factory=dict
    )
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )

    def increment_published(
        self,
        event_type: str,
        delivery_class: Optional[str] = None,
    ) -> None:
        """
        Increment published count for event type.

        Args:
            event_type: Name of event class (e.g., "AgentStarted")
        """
        with self._lock:
            self.published[event_type] += 1
            if delivery_class is not None:
                self.admitted_by_class[delivery_class] += 1

    def increment_dropped(
        self,
        event_type: str,
        delivery_class: Optional[str] = None,
    ) -> None:
        """
        Increment dropped count for event type.

        Called when queue is full and event cannot be queued.

        Args:
            event_type: Name of event class
        """
        with self._lock:
            self.dropped[event_type] += 1
            if delivery_class is not None:
                self.dropped_by_class[delivery_class] += 1

    def increment_admission_failed(self, delivery_class: str) -> None:
        """Record a non-lossy event that could not be admitted in time."""
        with self._lock:
            self.admission_failed_by_class[delivery_class] += 1

    def record_queue_depth(self, delivery_class: str, depth: int) -> None:
        """Record current and high-water queue depth for a delivery class."""
        with self._lock:
            self.queue_depth[delivery_class] = depth
            previous = self.queue_depth_high_water.get(delivery_class, 0)
            if depth > previous:
                self.queue_depth_high_water[delivery_class] = depth

    def increment_handler_failed(self, event_type: str, handler: str) -> None:
        """
        Increment handler failure count.

        Called when handler raises exception during event processing.

        Args:
            event_type: Name of event class
            handler: Name of handler class
        """
        key = f"{event_type}:{handler}"
        with self._lock:
            self.handler_failed[key] += 1

    def record_delivery(
        self,
        event_type: str,
        handler: str,
        duration_ms: float,
        delivery_class: Optional[str] = None,
    ) -> None:
        """
        Record successful event delivery with latency.

        Args:
            event_type: Name of event class
            handler: Name of handler class
            duration_ms: Time taken to deliver event (milliseconds)
        """
        with self._lock:
            self.delivered[event_type] += 1
            key = f"{event_type}:{handler}"
            self.delivery_duration_ms.setdefault(
                key,
                deque(maxlen=self.max_latency_samples),
            ).append(duration_ms)

    def record_class_delivery(
        self,
        delivery_class: str,
        latency_ms: float,
    ) -> None:
        """Record one event's admission-to-completion latency."""
        with self._lock:
            self.delivered_by_class[delivery_class] += 1
            self.delivery_latency_ms_by_class.setdefault(
                delivery_class,
                deque(maxlen=self.max_latency_samples),
            ).append(latency_ms)

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
        with self._lock:
            duration_by_handler = {
                key: list(values)
                for key, values in self.delivery_duration_ms.items()
            }
            duration_by_class = {
                key: list(values)
                for key, values in self.delivery_latency_ms_by_class.items()
            }
            return {
                "published": dict(self.published),
                "delivered": dict(self.delivered),
                "dropped": dict(self.dropped),
                "handler_failed": dict(self.handler_failed),
                "admitted_by_class": dict(self.admitted_by_class),
                "admission_failed_by_class": dict(
                    self.admission_failed_by_class
                ),
                "delivered_by_class": dict(self.delivered_by_class),
                "dropped_by_class": dict(self.dropped_by_class),
                "queue_depth": dict(self.queue_depth),
                "queue_depth_high_water": dict(self.queue_depth_high_water),
                "latency_sample_count": {
                    key: len(values)
                    for key, values in duration_by_handler.items()
                },
                "latency_sample_count_by_class": {
                    key: len(values)
                    for key, values in duration_by_class.items()
                },
                "p50_delivery_ms": {
                    k: statistics.median(v)
                    for k, v in duration_by_handler.items()
                    if v
                },
                "p99_delivery_ms": {
                    k: (
                        statistics.quantiles(v, n=100)[98]
                        if len(v) >= 100
                        else max(v)
                    )
                    for k, v in duration_by_handler.items()
                    if v
                },
                "p50_delivery_ms_by_class": {
                    k: statistics.median(v)
                    for k, v in duration_by_class.items()
                    if v
                },
                "p99_delivery_ms_by_class": {
                    k: (
                        statistics.quantiles(v, n=100)[98]
                        if len(v) >= 100
                        else max(v)
                    )
                    for k, v in duration_by_class.items()
                    if v
                },
            }

    def reset(self) -> None:
        """Clear all metrics (useful for testing)."""
        with self._lock:
            self.published.clear()
            self.delivered.clear()
            self.dropped.clear()
            self.handler_failed.clear()
            self.admitted_by_class.clear()
            self.admission_failed_by_class.clear()
            self.delivered_by_class.clear()
            self.dropped_by_class.clear()
            self.queue_depth.clear()
            self.queue_depth_high_water.clear()
            self.delivery_duration_ms.clear()
            self.delivery_latency_ms_by_class.clear()
