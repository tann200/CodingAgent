"""Shared queue/backpressure helpers for server event delivery."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional


EventQueueItem = tuple[str, Any]
DropCallback = Callable[[str], None]
ClientDropCallback = Callable[[str, str], None]


def record_dropped_event(
    event_name: str,
    session_id: str,
    *,
    inc_event_dropped_counter: DropCallback,
    inc_client_event_dropped_counter: ClientDropCallback,
) -> None:
    """Record a dropped event in both aggregate and per-client counters."""
    try:
        inc_event_dropped_counter(event_name)
        inc_client_event_dropped_counter(event_name, session_id)
    except Exception:
        pass


def enqueue_with_drop_policy(
    queue: asyncio.Queue,
    item: EventQueueItem,
    *,
    drop_policy: str,
    on_drop: DropCallback,
    on_evict: Optional[DropCallback] = None,
) -> bool:
    """Enqueue an item, applying drop-oldest or drop-new semantics on overflow."""
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        if drop_policy != "drop_oldest":
            on_drop(item[0])
            return False

    _evict_oldest_item(queue, on_evict=on_evict)
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        on_drop(item[0])
        return False


def _evict_oldest_item(
    queue: asyncio.Queue,
    *,
    on_evict: Optional[DropCallback] = None,
) -> None:
    """Best-effort eviction of the oldest queued item."""
    try:
        dropped = queue.get_nowait()
    except Exception:
        return

    if not dropped or on_evict is None:
        return

    try:
        dropped_name = dropped[0]
    except Exception:
        return

    if dropped_name:
        on_evict(str(dropped_name))
