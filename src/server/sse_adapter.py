"""Server-Sent Events adapter for the HTTP server.

Extracted from app.py (Phase G — thin facade cleanup).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator, Dict, Optional

from src.core.orchestration.event_bus import EventBus
from src.server.event_delivery import enqueue_with_drop_policy, record_dropped_event
from src.server.event_subscriptions import DEFAULT_SERVER_EVENT_TYPES
from src.server.metrics import inc_client_event_dropped_counter, inc_event_dropped_counter
from src.core.security.redaction import redact_data

logger = logging.getLogger(__name__)


class ServerEventBusAdapter:
    """Adapts internal EventBus to Server-Sent Events for HTTP clients.

    The adapter accepts configurable per-client queue sizing and keepalive
    interval so deployments can tune memory/latency tradeoffs.
    """

    def __init__(
        self,
        event_bus: EventBus,
        queue_max_size: int = 100,
        keepalive_interval: int = 15,
        drop_policy: str = "drop_oldest",
    ):
        self.event_bus = event_bus
        q_val = 100 if queue_max_size is None else int(queue_max_size)
        self.queue_max_size = max(1, min(q_val, 1000))
        k_val = 15 if keepalive_interval is None else int(keepalive_interval)
        self.keepalive_interval = max(1, min(k_val, 300))
        raw_policy = (drop_policy or "drop_oldest").lower()
        self.drop_policy = raw_policy if raw_policy in ("drop_oldest", "drop_new") else "drop_oldest"
        self._session_handlers: Dict[str, list] = {}

    async def event_generator(self, session_id: str) -> AsyncGenerator[str, None]:
        """Generate SSE events for a given session."""
        loop = asyncio.get_running_loop()
        connection_id = f"{session_id}:{uuid.uuid4().hex[:8]}"
        event_queue: asyncio.Queue[Optional[tuple]] = asyncio.Queue(
            maxsize=self.queue_max_size
        )

        def _record_dropped(event_name: str) -> None:
            record_dropped_event(
                event_name,
                session_id,
                inc_event_dropped_counter=inc_event_dropped_counter,
                inc_client_event_dropped_counter=inc_client_event_dropped_counter,
            )

        def make_handler(event_name: str):
            def handler(payload: dict) -> None:
                if session_id != "all":
                    if not isinstance(payload, dict) or payload.get("session_id") != session_id:
                        return
                policy = getattr(self, "drop_policy", "drop_oldest")

                def _enqueue() -> None:
                    if not enqueue_with_drop_policy(
                        event_queue,
                        (event_name, payload),
                        drop_policy=policy,
                        on_drop=_record_dropped,
                        on_evict=_record_dropped if policy == "drop_oldest" else None,
                    ):
                        if policy == "drop_oldest":
                            logger.warning(
                                "Event queue full for session %s, dropping event %s",
                                session_id,
                                event_name,
                            )
                        elif policy == "drop_new":
                            logger.warning(
                                "Event queue full for session %s, dropping new event %s",
                                session_id,
                                event_name,
                            )

                try:
                    loop.call_soon_threadsafe(_enqueue)
                except RuntimeError:
                    pass

            return handler

        key_event_types = list(DEFAULT_SERVER_EVENT_TYPES)
        handlers = []
        for event_type in key_event_types:
            handler = make_handler(event_type)
            handlers.append((event_type, handler))

        self._session_handlers[connection_id] = handlers
        for event_type, handler in handlers:
            self.event_bus.subscribe(event_type, handler)

        try:
            async def _keepalive_sender():
                try:
                    while True:
                        await asyncio.sleep(self.keepalive_interval)
                        enqueue_with_drop_policy(
                            event_queue,
                            ("_keepalive", {"comment": "ping"}),
                            drop_policy=getattr(self, "drop_policy", "drop_oldest"),
                            on_drop=_record_dropped,
                        )
                except asyncio.CancelledError:
                    return

            keepalive_task = asyncio.create_task(_keepalive_sender())
            try:
                while True:
                    result = await event_queue.get()
                    if result is None:
                        break
                    event_name, payload = result
                    data = json.dumps(
                        {
                            "event": event_name,
                            "data": redact_data(payload),
                        }
                    )
                    yield f"data: {data}\n\n"
            finally:
                try:
                    keepalive_task.cancel()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error in SSE event generator for session {session_id}: {e}")
        finally:
            if connection_id in self._session_handlers:
                for event_name, handler in list(
                    self._session_handlers.get(connection_id, [])
                ):
                    try:
                        self.event_bus.unsubscribe(event_name, handler)
                    except Exception:
                        pass
                try:
                    del self._session_handlers[connection_id]
                except Exception:
                    pass
            try:
                while not event_queue.empty():
                    _ = event_queue.get_nowait()
            except Exception:
                pass
