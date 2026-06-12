"""
Asynchronous message bus with typed event delivery.

Features:
- Type-safe event delivery
- Bounded queue with backpressure
- Error isolation (failed handlers don't kill bus)
- Delivery guarantees with explicit drop logging
- Metrics for observability
- Sequenced dispatch: critical event types (tool/step/delegation lifecycle)
  are delivered in publication order within their category

Architecture:
- Events are queued via a thread-safe ``queue.Queue`` (synchronous ``publish``)
- A bridge task transfers events to the event loop as they arrive
- Sequenced events are routed to per-category FIFO queues for ordered delivery
- Non-sequenced events are dispatched concurrently via ``run_in_executor``
- The event loop runs on a dedicated daemon thread
"""

import asyncio
import logging
import queue as sync_queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Type

from src.core.messaging.events import Event
from src.core.messaging.metrics import MessageBusMetrics

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sequenced event categories
# Events in the same category are delivered to handlers in FIFO publication
# order.  Categories are independent (tool events don't block delegation).
# ---------------------------------------------------------------------------
_SEQUENCED_CATEGORIES: Dict[Type[Event], str] = {}

# Lazy-populated on first access.  We populate outside the class to break
# the circular dep: bus.py cannot eagerly import event_types.py (which
# imports Event from events.py — a module that also references bus types).
def _get_sequenced_category(event_type: Type[Event]) -> Optional[str]:
    """Return the sequenced category for *event_type*, or None.

    Categories (defined below) ensure that tool lifecycle, step lifecycle,
    and delegation lifecycle events are always delivered in order within
    their group.  All other events are non-sequenced and may be dispatched
    concurrently.
    """
    global _SEQUENCED_CATEGORIES
    if not _SEQUENCED_CATEGORIES:
        from src.core.messaging.event_types import (
            DelegationFinish,
            DelegationStart,
            StepFinish,
            StepStart,
            ToolExecuteError,
            ToolExecuteFinish,
            ToolExecuteStart,
        )

        _SEQUENCED_CATEGORIES = {
            ToolExecuteStart: "tool",
            ToolExecuteFinish: "tool",
            ToolExecuteError: "tool",
            StepStart: "step",
            StepFinish: "step",
            DelegationStart: "delegation",
            DelegationFinish: "delegation",
        }
    return _SEQUENCED_CATEGORIES.get(event_type)


class EventHandler(Protocol):
    def handle(self, event: Event) -> None:
        ...


@dataclass
class _SyncDelivery:
    event: Event
    delivered: threading.Event


class MessageBus:
    def __init__(
        self,
        max_queue_size: int = 1000,
        worker_threads: int = 1,
        enable_metrics: bool = True,
    ):
        self._max_queue_size = max_queue_size
        self._worker_count = worker_threads
        self._enable_metrics = enable_metrics
        self._metrics = MessageBusMetrics()

        self._handlers: Dict[Type[Event], List[EventHandler]] = {}
        self._lock = threading.RLock()

        self._shutdown_flag = threading.Event()

        # Thread-safe sync queue — publish() writes from caller thread
        self._sync_queue: sync_queue.Queue = sync_queue.Queue(maxsize=max_queue_size)

        # Async infrastructure runs on daemon loop thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._bridge_task: Optional[asyncio.Task] = None
        self._started = threading.Event()

        # Sequenced dispatch: per-category FIFO queues + consumer tasks.
        # Populated lazily when the first sequenced event arrives.
        self._seq_queues: Dict[str, asyncio.Queue] = {}
        self._seq_consumers: Dict[str, asyncio.Task] = {}

        self._start()

    def _start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name="MessageBus-EventLoop",
            daemon=True,
        )
        self._loop_thread.start()
        self._started.wait(timeout=10.0)
        if not self._started.is_set():
            logger.error("MessageBus: event loop thread failed to start")

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._bridge_task = self._loop.create_task(self._bridge())
        for i in range(self._worker_count):
            self._loop.create_task(self._worker(i))
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _bridge(self) -> None:
        """Transfer items from sync queue to event loop for async dispatch.

        Runs as a task on the event loop. Blocks on the sync queue using
        run_in_executor so the event loop stays responsive. When an item
        arrives, it dispatches it:
        - Sequenced events (tool/step/delegation lifecycle) go to per-category
          FIFO queues for ordered delivery.
        - Non-sequenced events are dispatched immediately.
        """
        assert self._loop is not None
        while not self._shutdown_flag.is_set():
            try:
                item = await asyncio.get_event_loop().run_in_executor(
                    None, self._sync_queue.get, True, 0.5
                )
            except sync_queue.Empty:
                continue
            except Exception as e:
                logger.error(
                    "MessageBus: bridge error: %s", e, exc_info=True
                )
                continue

            if isinstance(item, Event):
                seq_cat = _get_sequenced_category(type(item))
                if seq_cat is not None:
                    await self._seq_put(item, seq_cat)
                    continue

            self._loop.create_task(self._dispatch(item))

        # Drain remaining items after shutdown
        while True:
            try:
                item = self._sync_queue.get_nowait()
            except sync_queue.Empty:
                break
            if isinstance(item, Event):
                seq_cat = _get_sequenced_category(type(item))
                if seq_cat is not None:
                    self._loop.create_task(self._dispatch(item))
                    continue
            self._loop.create_task(self._dispatch(item))

    async def _dispatch(self, item: Any) -> None:
        """Deliver a single item to handlers (runs as a one-shot task)."""
        if isinstance(item, _SyncDelivery):
            await asyncio.get_event_loop().run_in_executor(
                None, self._deliver_to_handlers, item.event
            )
            item.delivered.set()
        elif callable(item):
            try:
                await asyncio.get_event_loop().run_in_executor(None, item)
            except Exception as e:
                logger.error(
                    "MessageBus: sync delivery function failed: %s",
                    e, exc_info=True,
                )
        else:
            await asyncio.get_event_loop().run_in_executor(
                None, self._deliver_to_handlers, item
            )

    async def _seq_put(self, event: Event, category: str) -> None:
        """Enqueue a sequenced event for ordered delivery within *category*.

        Lazily creates the per-category ``asyncio.Queue`` and consumer task
        on first use.
        """
        assert self._loop is not None
        if category not in self._seq_queues:
            q: asyncio.Queue = asyncio.Queue()
            self._seq_queues[category] = q
            self._seq_consumers[category] = self._loop.create_task(
                self._seq_consumer(category, q)
            )
        await self._seq_queues[category].put(event)

    async def _seq_consumer(self, category: str, q: asyncio.Queue) -> None:
        """Consume sequenced events from *q* one at a time, in FIFO order.

        Only one event per category is dispatched at a time, ensuring
        handlers see them in publication order.  Categories are independent
        (a slow tool event does not block delegation events).
        """
        while not self._shutdown_flag.is_set():
            try:
                event = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(
                    "MessageBus: seq_consumer(%s) error: %s", category, e, exc_info=True
                )
                continue

            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._deliver_to_handlers, event
                )
            except Exception as e:
                logger.error(
                    "MessageBus: seq_consumer(%s) dispatch failed: %s",
                    category, e, exc_info=True,
                )

    async def _worker(self, worker_id: int) -> None:
        """Legacy worker stub — dispatch now handled by _bridge + _dispatch.

        Kept for API compatibility and to match the worker_threads count.
        Will be removed when all callers migrate to the async API.
        """
        # Workers are no longer needed as individual consumers — _bridge
        # creates _dispatch tasks. Keep the task alive until shutdown so
        # the worker_threads count is respected for backward compatibility.
        await asyncio.get_event_loop().run_in_executor(
            None, self._shutdown_flag.wait
        )

    def subscribe(
        self, event_type: Type[Event], handler: EventHandler
    ) -> None:
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
        if self._shutdown_flag.is_set():
            logger.warning(
                "MessageBus: publish after shutdown, dropping: %s", event
            )
            return

        try:
            self._sync_queue.put_nowait(event)
            if self._enable_metrics:
                self._metrics.increment_published(type(event).__name__)
        except sync_queue.Full:
            logger.error(
                "MessageBus: queue full (%d events), dropping: %s",
                self._sync_queue.qsize(),
                event,
            )
            if self._enable_metrics:
                self._metrics.increment_dropped(type(event).__name__)

    def publish_sync(self, event: Event, timeout: float = 5.0) -> bool:
        if self._shutdown_flag.is_set():
            logger.warning(
                "MessageBus: publish_sync after shutdown, dropping: %s", event
            )
            return False

        delivery = _SyncDelivery(
            event=event,
            delivered=threading.Event(),
        )
        try:
            self._sync_queue.put(delivery, timeout=timeout)
            if self._enable_metrics:
                self._metrics.increment_published(type(event).__name__)
        except sync_queue.Full:
            logger.error("MessageBus: sync publish queue full")
            if self._enable_metrics:
                self._metrics.increment_dropped(type(event).__name__)
            return False

        return delivery.delivered.wait(timeout=timeout)

    def _deliver_to_handlers(self, event: Event) -> None:
        event_type = type(event)

        with self._lock:
            handlers = self._handlers.get(event_type, []).copy()

        if not handlers:
            logger.debug(
                "MessageBus: no handlers for %s", event_type.__name__
            )
            return

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
        logger.info("MessageBus: shutting down...")
        self._shutdown_flag.set()

        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._async_shutdown(), self._loop
            )

        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=timeout)
            if self._loop_thread.is_alive():
                logger.warning(
                    "MessageBus: loop thread did not stop within %.1fs",
                    timeout,
                )

        logger.info("MessageBus: shutdown complete")

    async def _async_shutdown(self) -> None:
        assert self._loop is not None
        me = asyncio.current_task()
        all_tasks = asyncio.all_tasks(self._loop)
        others = [t for t in all_tasks if t is not me]
        if others:
            await asyncio.wait(others, timeout=10.0)
        self._loop.stop()

    def get_metrics(self) -> Dict[str, Any]:
        return self._metrics.snapshot()

    def reset_metrics(self) -> None:
        self._metrics.reset()
