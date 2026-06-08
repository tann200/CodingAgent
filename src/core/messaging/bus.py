"""
Asynchronous message bus with typed event delivery.

Features:
- Type-safe event delivery
- Bounded queue with backpressure
- Error isolation (failed handlers don't kill bus)
- Delivery guarantees with explicit drop logging
- Metrics for observability
"""

import logging
import threading
import time
from queue import Empty, Full, Queue
from threading import Thread
from typing import Callable, Dict, List, Protocol, Type

from src.core.messaging.events import Event
from src.core.messaging.metrics import MessageBusMetrics

logger = logging.getLogger(__name__)


class EventHandler(Protocol):
    """
    Protocol for event handlers.
    
    Handlers receive events from the MessageBus and perform side effects
    (update UI, write logs, send metrics, etc.).
    
    Implementations should:
    - Not raise exceptions (catch and log internally)
    - Return quickly (offload heavy work to threads)
    - Be idempotent (may receive duplicate events)
    
    Example:
        ```python
        class MyHandler:
            def handle(self, event: Event) -> None:
                if isinstance(event, AgentStarted):
                    print(f"Agent {event.session_id} started: {event.task}")
        
        bus = MessageBus()
        bus.subscribe(AgentStarted, MyHandler())
        ```
    """
    
    def handle(self, event: Event) -> None:
        """
        Handle incoming event.
        
        Args:
            event: The event to handle
        """
        ...


class MessageBus:
    """
    Asynchronous message bus with typed events.
    
    Events are queued and delivered asynchronously by worker threads.
    Failed handlers are isolated - exceptions don't kill the worker or
    affect other handlers.
    
    Features:
    - Type-safe event delivery
    - Bounded queue with backpressure
    - Error isolation (failed handlers don't kill bus)
    - Delivery guarantees with explicit drop logging
    - Metrics for observability
    
    Example:
        ```python
        # Create bus
        bus = MessageBus(max_queue_size=1000, worker_threads=1)
        
        # Subscribe handler
        class MyHandler:
            def handle(self, event: Event):
                print(f"Received: {event}")
        
        bus.subscribe(AgentStarted, MyHandler())
        
        # Publish event
        bus.publish(AgentStarted(
            session_id="sess_123",
            role="operational",
            task="Fix bug"
        ))
        
        # Shutdown gracefully
        bus.shutdown()
        ```
    
    Thread Safety:
        All public methods are thread-safe. Multiple threads can publish
        and subscribe concurrently.
    
    Backpressure:
        If queue is full, events are dropped and logged at ERROR level.
        Metric `dropped` is incremented for monitoring.
    
    Error Handling:
        Handler exceptions are caught, logged at WARNING level, and
        metric `handler_failed` is incremented. Other handlers continue
        processing.
    """
    
    def __init__(
        self,
        max_queue_size: int = 1000,
        worker_threads: int = 1,
        enable_metrics: bool = True,
    ):
        """
        Initialize MessageBus.
        
        Args:
            max_queue_size: Maximum events in queue before backpressure
            worker_threads: Number of worker threads for event delivery
            enable_metrics: Whether to collect metrics
        """
        self._queue: Queue[Event] = Queue(maxsize=max_queue_size)
        self._handlers: Dict[Type[Event], List[EventHandler]] = {}
        self._lock = threading.RLock()
        self._shutdown_flag = threading.Event()
        self._workers: List[Thread] = []
        self._enable_metrics = enable_metrics
        
        # Metrics
        self._metrics = MessageBusMetrics()
        
        # Start worker threads
        for i in range(worker_threads):
            worker = Thread(
                target=self._process_events,
                name=f"MessageBus-Worker-{i}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
            logger.info("MessageBus: started worker thread %s", worker.name)
    
    def subscribe(
        self, event_type: Type[Event], handler: EventHandler
    ) -> None:
        """
        Register a handler for specific event type.
        
        The handler will be called asynchronously whenever an event of
        the specified type is published.
        
        Args:
            event_type: The Event class to subscribe to
            handler: Object with handle(event) method
        
        Example:
            ```python
            bus.subscribe(AgentStarted, MyHandler())
            bus.subscribe(ToolCallFinished, MyHandler())
            ```
        
        Thread Safety:
            Safe to call from any thread.
        """
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)
            logger.debug(
                "MessageBus: subscribed %s to %s",
                handler.__class__.__name__,
                event_type.__name__,
            )
    
    def unsubscribe(
        self, event_type: Type[Event], handler: EventHandler
    ) -> None:
        """
        Remove handler subscription.
        
        Args:
            event_type: The Event class to unsubscribe from
            handler: The handler to remove
        
        Thread Safety:
            Safe to call from any thread.
        """
        with self._lock:
            if event_type in self._handlers:
                try:
                    self._handlers[event_type].remove(handler)
                    logger.debug(
                        "MessageBus: unsubscribed %s from %s",
                        handler.__class__.__name__,
                        event_type.__name__,
                    )
                except ValueError:
                    logger.warning(
                        "MessageBus: handler %s not found for %s",
                        handler.__class__.__name__,
                        event_type.__name__,
                    )
    
    def publish(self, event: Event) -> None:
        """
        Publish event for async delivery.
        
        Events are queued and delivered by worker thread.
        If queue is full, event is dropped and logged at ERROR level.
        
        Args:
            event: The event to publish
        
        Example:
            ```python
            bus.publish(AgentStarted(
                session_id="sess_123",
                role="operational",
                task="Fix bug"
            ))
            ```
        
        Thread Safety:
            Safe to call from any thread.
        
        Backpressure:
            If queue is full, event is dropped (not blocking).
            Metric `dropped` is incremented.
        """
        if self._shutdown_flag.is_set():
            logger.warning(
                "MessageBus: publish after shutdown, dropping: %s", event
            )
            return
        
        try:
            self._queue.put_nowait(event)
            if self._enable_metrics:
                self._metrics.increment_published(type(event).__name__)
        except Full:
            logger.error(
                "MessageBus: queue full (%d events), dropping: %s",
                self._queue.qsize(),
                event,
            )
            if self._enable_metrics:
                self._metrics.increment_dropped(type(event).__name__)
    
    def publish_sync(self, event: Event, timeout: float = 5.0) -> bool:
        """
        Publish event and wait for delivery (blocking).
        
        This is useful for testing or when you need to ensure event
        is delivered before proceeding.
        
        Args:
            event: The event to publish
            timeout: Maximum time to wait for delivery (seconds)
        
        Returns:
            True if delivered, False if timeout or dropped
        
        Example:
            ```python
            success = bus.publish_sync(
                AgentStarted(...),
                timeout=2.0
            )
            if not success:
                print("Event delivery timed out or queue full")
            ```
        
        Thread Safety:
            Safe to call from any thread (but will block caller).
        """
        if self._shutdown_flag.is_set():
            logger.warning(
                "MessageBus: publish_sync after shutdown, dropping: %s", event
            )
            return False
        
        delivered = threading.Event()
        event_type_name = type(event).__name__

        def deliver_and_signal() -> None:
            try:
                self._deliver_to_handlers(event)
            finally:
                delivered.set()

        try:
            # Queue the delivery function
            self._queue.put(deliver_and_signal, timeout=timeout)  # type: ignore
            if self._enable_metrics:
                self._metrics.increment_published(event_type_name)
            # Wait for delivery to complete
            return delivered.wait(timeout=timeout)
        except Full:
            logger.error("MessageBus: sync publish queue full")
            if self._enable_metrics:
                self._metrics.increment_dropped(event_type_name)
            return False
    
    def _process_events(self) -> None:
        """
        Worker thread: dequeue and deliver events.

        Runs until shutdown flag is set AND the queue is empty.
        After the shutdown flag is set the worker keeps draining so that
        events published before shutdown() was called are still delivered.
        Catches all exceptions to ensure the worker thread stays alive.
        """
        logger.info(
            "MessageBus worker started: %s", threading.current_thread().name
        )

        def _process_one(item: object) -> None:
            # Handle callable (from publish_sync)
            if callable(item):
                try:
                    item()
                except Exception as e:
                    logger.error(
                        "MessageBus: sync delivery function failed: %s",
                        e,
                        exc_info=True,
                    )
                finally:
                    self._queue.task_done()
                return

            # Handle Event
            try:
                self._deliver_to_handlers(item)  # type: ignore[arg-type]
            except Exception as e:
                logger.error(
                    "MessageBus: unexpected error delivering %s: %s",
                    type(item).__name__,
                    e,
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

        while True:
            # Normal operation: block until an item arrives or poll period elapses.
            if not self._shutdown_flag.is_set():
                try:
                    item = self._queue.get(timeout=0.5)
                    _process_one(item)
                except Empty:
                    continue
            else:
                # Shutdown requested: drain remaining items without blocking,
                # then exit the loop.
                try:
                    item = self._queue.get_nowait()
                    _process_one(item)
                except Empty:
                    break  # queue fully drained — worker can stop

        logger.info(
            "MessageBus worker stopped: %s", threading.current_thread().name
        )
    
    def _deliver_to_handlers(self, event: Event) -> None:
        """
        Deliver event to all registered handlers.
        
        Handlers are called sequentially. If a handler raises an exception,
        it is caught and logged, and other handlers continue processing.
        
        Args:
            event: The event to deliver
        """
        event_type = type(event)
        
        # Get handler list (copy to avoid holding lock during delivery)
        with self._lock:
            handlers = self._handlers.get(event_type, []).copy()
        
        if not handlers:
            logger.debug("MessageBus: no handlers for %s", event_type.__name__)
            return
        
        # Deliver to each handler
        for handler in handlers:
            try:
                start = time.perf_counter()
                handler.handle(event)
                duration_ms = (time.perf_counter() - start) * 1000
                
                if self._enable_metrics:
                    self._metrics.record_delivery(
                        event_type.__name__,
                        handler.__class__.__name__,
                        duration_ms,
                    )
                
                logger.debug(
                    "MessageBus: delivered %s to %s (%.2fms)",
                    event_type.__name__,
                    handler.__class__.__name__,
                    duration_ms,
                )
                
            except Exception as e:
                logger.warning(
                    "MessageBus: handler %s failed for %s: %s",
                    handler.__class__.__name__,
                    event_type.__name__,
                    e,
                    exc_info=True,
                )
                if self._enable_metrics:
                    self._metrics.increment_handler_failed(
                        event_type.__name__, handler.__class__.__name__
                    )
    
    def shutdown(self, timeout: float = 10.0) -> None:
        """
        Gracefully shutdown message bus.
        
        Waits for queue to drain, then stops workers.
        
        Args:
            timeout: Maximum time to wait for shutdown (seconds)
        
        Example:
            ```python
            bus.shutdown(timeout=5.0)
            ```
        
        Thread Safety:
            Safe to call from any thread.
        """
        logger.info("MessageBus: shutting down...")
        self._shutdown_flag.set()

        # Workers will drain remaining queued items after seeing the shutdown
        # flag, then exit.  We join them with a bounded timeout so we never
        # block forever if a handler is pathologically slow/stuck.
        worker_timeout = timeout / max(len(self._workers), 1)
        for worker in self._workers:
            worker.join(timeout=worker_timeout)
            if worker.is_alive():
                logger.warning(
                    "MessageBus: worker %s did not stop within %.1fs — "
                    "a handler may be blocked",
                    worker.name,
                    worker_timeout,
                )

        logger.info("MessageBus: shutdown complete")
    
    def get_metrics(self) -> Dict[str, any]:
        """
        Return current metrics snapshot.
        
        Returns:
            Dictionary with all metrics (published, delivered, dropped, etc.)
        
        Example:
            ```python
            metrics = bus.get_metrics()
            print(f"Published: {metrics['published']}")
            print(f"Dropped: {metrics['dropped']}")
            print(f"P99 latency: {metrics['p99_delivery_ms']}")
            ```
        """
        return self._metrics.snapshot()
    
    def reset_metrics(self) -> None:
        """Reset all metrics (useful for testing)."""
        self._metrics.reset()
