"""
Asynchronous message bus with typed event delivery.

Features:
- Type-safe event delivery
- Per-class bounded queues with explicit backpressure
- Error isolation (failed handlers don't kill bus)
- Reliable and ordered events fail loudly instead of being dropped
- Lossy telemetry remains bounded with explicit drop metrics
- Queue depth and delivery metrics are reported by delivery class
- Sequenced dispatch: critical event types (tool/step/delegation lifecycle)
  are delivered in publication order within their category

Architecture:
- Events are admitted to delivery-class-specific thread-safe queues
- One bridge per class prevents telemetry handlers from blocking reliable events
- Sequenced events are routed to per-category FIFO queues for ordered delivery
- Reliable and telemetry dispatch use independent concurrency limits
- The event loop runs on a dedicated daemon thread
"""

import asyncio
import logging
import queue as sync_queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Set, Type

from src.core.messaging.events import (
    Event,
    EventDeliveryClass,
    EventDeliveryPolicy,
    get_event_delivery_policy,
)
from src.core.messaging.metrics import MessageBusMetrics

logger = logging.getLogger(__name__)
_SEQUENCE_STOP = object()


class ReliableEventAdmissionError(RuntimeError):
    """Raised when a non-lossy event cannot enter its bounded queue."""


class EventDeliveryTimeoutError(TimeoutError):
    """Raised when an admitted non-lossy event is not acknowledged in time."""


class MessageBusShutdownError(TimeoutError):
    """Raised when graceful shutdown has not drained by its deadline."""


class EventHandler(Protocol):
    def handle(self, event: Event) -> None:
        ...


@dataclass
class _DeliveryItem:
    event: Event
    admitted_at: float
    delivered: Optional[threading.Event] = None


class MessageBus:
    def __init__(
        self,
        max_queue_size: int = 1000,
        worker_threads: int = 1,
        enable_metrics: bool = True,
        reliable_publish_timeout: float = 5.0,
    ):
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        if worker_threads < 1:
            raise ValueError("worker_threads must be at least 1")
        if reliable_publish_timeout < 0:
            raise ValueError("reliable_publish_timeout cannot be negative")
        self._max_queue_size = max_queue_size
        self._worker_count = worker_threads
        self._enable_metrics = enable_metrics
        self._reliable_publish_timeout = reliable_publish_timeout
        self._metrics = MessageBusMetrics()

        self._handlers: Dict[Type[Event], List[EventHandler]] = {}
        self._lock = threading.RLock()
        self._admission_condition = threading.Condition()
        self._active_admissions: Counter = Counter()
        self._inflight_lock = threading.Lock()
        self._inflight_by_class: Counter = Counter()

        self._shutdown_flag = threading.Event()
        self._shutdown_future: Optional[Any] = None

        # Independent bounded admission lanes prevent telemetry saturation from
        # consuming reliable/lifecycle capacity.
        self._queues: Dict[EventDeliveryClass, sync_queue.Queue] = {
            delivery_class: sync_queue.Queue(maxsize=max_queue_size)
            for delivery_class in EventDeliveryClass
        }

        # Async infrastructure runs on daemon loop thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._bridge_tasks: Dict[EventDeliveryClass, asyncio.Task] = {}
        self._lane_events: Dict[EventDeliveryClass, asyncio.Event] = {}
        self._dispatch_semaphores: Dict[
            EventDeliveryClass, asyncio.Semaphore
        ] = {}
        self._dispatch_tasks: Set[asyncio.Task] = set()
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
        self._dispatch_semaphores = {
            EventDeliveryClass.TELEMETRY: asyncio.Semaphore(self._worker_count),
            EventDeliveryClass.RELIABLE: asyncio.Semaphore(self._worker_count),
        }
        self._lane_events = {
            delivery_class: asyncio.Event()
            for delivery_class in EventDeliveryClass
        }
        self._bridge_tasks = {
            delivery_class: self._loop.create_task(
                self._bridge(delivery_class),
                name=f"message-bus-{delivery_class.value}-bridge",
            )
            for delivery_class in EventDeliveryClass
        }
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    @staticmethod
    def _event_from_item(item: Any) -> Optional[Event]:
        if isinstance(item, _DeliveryItem):
            return item.event
        return None

    def _change_inflight(
        self,
        delivery_class: EventDeliveryClass,
        delta: int,
    ) -> None:
        with self._inflight_lock:
            self._inflight_by_class[delivery_class.value] += delta
            if self._inflight_by_class[delivery_class.value] <= 0:
                self._inflight_by_class.pop(delivery_class.value, None)
        self._record_pipeline_depth(delivery_class)

    def _record_pipeline_depth(
        self,
        delivery_class: EventDeliveryClass,
    ) -> None:
        if not self._enable_metrics:
            return
        with self._inflight_lock:
            inflight = self._inflight_by_class.get(delivery_class.value, 0)
        depth = self._queues[delivery_class].qsize() + inflight
        self._metrics.record_queue_depth(delivery_class.value, depth)

    def _begin_admission(
        self,
        delivery_class: EventDeliveryClass,
    ) -> bool:
        with self._admission_condition:
            if self._shutdown_flag.is_set():
                return False
            self._active_admissions[delivery_class.value] += 1
            return True

    def _end_admission(
        self,
        delivery_class: EventDeliveryClass,
    ) -> None:
        with self._admission_condition:
            self._active_admissions[delivery_class.value] -= 1
            if self._active_admissions[delivery_class.value] <= 0:
                self._active_admissions.pop(delivery_class.value, None)
            self._admission_condition.notify_all()
        self._wake_lane(delivery_class)

    def _has_active_admission(
        self,
        delivery_class: EventDeliveryClass,
    ) -> bool:
        with self._admission_condition:
            return self._active_admissions.get(delivery_class.value, 0) > 0

    def _wake_lane(self, delivery_class: EventDeliveryClass) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        lane_event = self._lane_events.get(delivery_class)
        if lane_event is None:
            return
        try:
            self._loop.call_soon_threadsafe(lane_event.set)
        except RuntimeError:
            pass

    async def _bridge(self, delivery_class: EventDeliveryClass) -> None:
        """Drain one admission lane without blocking the other classes."""
        lane_queue = self._queues[delivery_class]
        lane_event = self._lane_events[delivery_class]
        semaphore = self._dispatch_semaphores.get(delivery_class)

        while True:
            acquired_slot = False
            if semaphore is not None:
                await semaphore.acquire()
                acquired_slot = True

            try:
                item = lane_queue.get_nowait()
            except sync_queue.Empty:
                if acquired_slot and semaphore is not None:
                    semaphore.release()
                lane_event.clear()
                if (
                    self._shutdown_flag.is_set()
                    and lane_queue.empty()
                    and not self._has_active_admission(delivery_class)
                ):
                    break
                if lane_queue.empty():
                    await lane_event.wait()
                continue

            if self._enable_metrics:
                self._change_inflight(delivery_class, 1)
            else:
                with self._inflight_lock:
                    self._inflight_by_class[delivery_class.value] += 1

            try:
                event = self._event_from_item(item)
                policy = (
                    get_event_delivery_policy(type(event))
                    if event is not None
                    else EventDeliveryPolicy(delivery_class)
                )
                if (
                    policy.delivery_class is EventDeliveryClass.ORDERED
                    and policy.sequence_category is not None
                ):
                    await self._seq_put(item, policy.sequence_category)
                else:
                    assert semaphore is not None
                    task = self._loop.create_task(
                        self._dispatch_with_slot(
                            item,
                            semaphore,
                            delivery_class,
                        )
                    )
                    self._track_dispatch_task(task)
                    acquired_slot = False
            finally:
                lane_queue.task_done()
                if acquired_slot and semaphore is not None:
                    semaphore.release()

    def _track_dispatch_task(self, task: asyncio.Task) -> None:
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def _dispatch_with_slot(
        self,
        item: Any,
        semaphore: asyncio.Semaphore,
        delivery_class: EventDeliveryClass,
    ) -> None:
        try:
            await self._dispatch(item)
        finally:
            self._change_inflight(delivery_class, -1)
            semaphore.release()

    async def _dispatch(self, item: Any) -> None:
        """Deliver a single item to handlers (runs as a one-shot task)."""
        if isinstance(item, _DeliveryItem):
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._deliver_to_handlers,
                    item.event,
                )
            finally:
                if self._enable_metrics:
                    delivery_class = get_event_delivery_policy(
                        type(item.event)
                    ).delivery_class.value
                    latency_ms = (time.perf_counter() - item.admitted_at) * 1000
                    self._metrics.record_class_delivery(
                        delivery_class,
                        latency_ms,
                    )
                if item.delivered is not None:
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

    async def _seq_put(self, item: Any, category: str) -> None:
        """Enqueue a sequenced event for ordered delivery within *category*.

        Lazily creates the per-category ``asyncio.Queue`` and consumer task
        on first use.
        """
        assert self._loop is not None
        if category not in self._seq_queues:
            q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
            self._seq_queues[category] = q
            self._seq_consumers[category] = self._loop.create_task(
                self._seq_consumer(category, q),
                name=f"message-bus-{category}-consumer",
            )
        await self._seq_queues[category].put(item)

    async def _seq_consumer(self, category: str, q: asyncio.Queue) -> None:
        """Consume sequenced events from *q* one at a time, in FIFO order.

        Only one event per category is dispatched at a time, ensuring
        handlers see them in publication order.  Categories are independent
        (a slow tool event does not block delegation events).
        """
        while True:
            item = await q.get()
            if item is _SEQUENCE_STOP:
                q.task_done()
                return

            try:
                await self._dispatch(item)
            except Exception as e:
                if "cannot schedule new futures" in str(e):
                    self._shutdown_flag.set()
                    return
                logger.error(
                    "MessageBus: seq_consumer(%s) dispatch failed: %s",
                    category, e, exc_info=True,
                )
            finally:
                self._change_inflight(EventDeliveryClass.ORDERED, -1)
                q.task_done()

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

    def publish(
        self,
        event: Event,
        timeout: Optional[float] = None,
    ) -> bool:
        """Publish an event according to its delivery policy.

        Telemetry uses non-blocking admission and returns ``False`` when full.
        Reliable and ordered events wait for bounded capacity and raise
        ``ReliableEventAdmissionError`` if that capacity cannot be obtained.
        """
        policy = get_event_delivery_policy(type(event))
        delivery_class = policy.delivery_class
        lane_queue = self._queues[delivery_class]
        item = _DeliveryItem(event=event, admitted_at=time.perf_counter())
        if not self._begin_admission(delivery_class):
            logger.warning(
                "MessageBus: publish after shutdown, rejecting: %s", event
            )
            return False
        try:
            if delivery_class is EventDeliveryClass.TELEMETRY:
                lane_queue.put_nowait(item)
            else:
                admission_timeout = (
                    self._reliable_publish_timeout
                    if timeout is None
                    else timeout
                )
                lane_queue.put(item, timeout=admission_timeout)
            if self._enable_metrics:
                self._metrics.increment_published(
                    type(event).__name__,
                    delivery_class.value,
                )
                self._record_pipeline_depth(delivery_class)
            return True
        except sync_queue.Full:
            if delivery_class is EventDeliveryClass.TELEMETRY:
                logger.warning(
                    "MessageBus: telemetry queue full (%d events), dropping: %s",
                    lane_queue.qsize(),
                    event,
                )
                if self._enable_metrics:
                    self._metrics.increment_dropped(
                        type(event).__name__,
                        delivery_class.value,
                    )
                return False

            if self._enable_metrics:
                self._metrics.increment_admission_failed(delivery_class.value)
            raise ReliableEventAdmissionError(
                f"MessageBus {delivery_class.value} queue remained full "
                f"at {lane_queue.qsize()} events; {type(event).__name__} "
                "was not admitted"
            ) from None
        finally:
            self._end_admission(delivery_class)

    def publish_sync(self, event: Event, timeout: float = 5.0) -> bool:
        delivery = _DeliveryItem(
            event=event,
            admitted_at=time.perf_counter(),
            delivered=threading.Event(),
        )
        policy = get_event_delivery_policy(type(event))
        delivery_class = policy.delivery_class
        lane_queue = self._queues[delivery_class]
        if not self._begin_admission(delivery_class):
            logger.warning(
                "MessageBus: publish_sync after shutdown, rejecting: %s",
                event,
            )
            return False
        try:
            lane_queue.put(delivery, timeout=timeout)
            if self._enable_metrics:
                self._metrics.increment_published(
                    type(event).__name__,
                    delivery_class.value,
                )
                self._record_pipeline_depth(delivery_class)
        except sync_queue.Full:
            logger.error(
                "MessageBus: synchronous %s publish queue full",
                delivery_class.value,
            )
            if delivery_class is EventDeliveryClass.TELEMETRY:
                if self._enable_metrics:
                    self._metrics.increment_dropped(
                        type(event).__name__,
                        delivery_class.value,
                    )
                return False

            if self._enable_metrics:
                self._metrics.increment_admission_failed(
                    delivery_class.value
                )
            raise ReliableEventAdmissionError(
                f"MessageBus {delivery_class.value} queue remained full "
                f"at {lane_queue.qsize()} events; {type(event).__name__} "
                "was not admitted synchronously"
            ) from None
        finally:
            self._end_admission(delivery_class)

        assert delivery.delivered is not None
        if delivery.delivered.wait(timeout=timeout):
            return True
        if delivery_class is EventDeliveryClass.TELEMETRY:
            return False
        raise EventDeliveryTimeoutError(
            f"{type(event).__name__} was admitted to the "
            f"{delivery_class.value} lane but was not acknowledged "
            f"within {timeout:.3f}s; delivery may still complete"
        )

    def _deliver_to_handlers(self, event: Event) -> None:
        event_type = type(event)
        delivery_class = get_event_delivery_policy(event_type).delivery_class.value

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
                        delivery_class,
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
        if threading.current_thread() is self._loop_thread:
            raise MessageBusShutdownError(
                "MessageBus.shutdown() cannot block its own event-loop thread"
            )

        with self._admission_condition:
            if not self._shutdown_flag.is_set():
                logger.info("MessageBus: shutting down...")
                self._shutdown_flag.set()
                self._admission_condition.notify_all()
                if self._loop is not None and not self._loop.is_closed():
                    self._shutdown_future = asyncio.run_coroutine_threadsafe(
                        self._async_shutdown(),
                        self._loop,
                    )
                    self._shutdown_future.add_done_callback(
                        self._stop_loop_after_drain
                    )
            shutdown_future = self._shutdown_future
        for delivery_class in EventDeliveryClass:
            self._wake_lane(delivery_class)

        if shutdown_future is not None:
            try:
                shutdown_future.result(timeout=timeout)
            except TimeoutError as exc:
                raise MessageBusShutdownError(
                    f"MessageBus did not drain within {timeout:.1f}s; "
                    "the loop remains active and will stop after draining"
                ) from exc
            except Exception as exc:
                raise MessageBusShutdownError(
                    f"MessageBus graceful drain failed: {exc}"
                ) from exc

        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=timeout)
            if self._loop_thread.is_alive():
                raise MessageBusShutdownError(
                    f"MessageBus drained but its loop did not stop within "
                    f"{timeout:.1f}s"
                )

        logger.info("MessageBus: shutdown complete")

    def _stop_loop_after_drain(self, shutdown_future: Any) -> None:
        try:
            shutdown_future.result()
        except Exception:
            logger.error(
                "MessageBus: drain failed; event loop left running",
                exc_info=True,
            )
            return
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass

    async def _async_shutdown(self) -> None:
        while True:
            with self._admission_condition:
                active_admissions = sum(self._active_admissions.values())
            if active_admissions == 0:
                break
            await asyncio.sleep(0.01)

        if self._bridge_tasks:
            await asyncio.gather(
                *self._bridge_tasks.values(),
                return_exceptions=True,
            )

        for queue in self._seq_queues.values():
            await queue.put(_SEQUENCE_STOP)

        for queue in self._seq_queues.values():
            await queue.join()

        pending_dispatch = list(self._dispatch_tasks)
        if pending_dispatch:
            await asyncio.gather(*pending_dispatch, return_exceptions=True)

        consumers = list(self._seq_consumers.values())
        if consumers:
            await asyncio.gather(*consumers, return_exceptions=True)

    def get_metrics(self) -> Dict[str, Any]:
        if self._enable_metrics:
            for delivery_class in self._queues:
                self._record_pipeline_depth(delivery_class)
        return self._metrics.snapshot()

    def reset_metrics(self) -> None:
        self._metrics.reset()
