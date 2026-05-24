"""WebSocket session endpoint for EventBus-based real-time updates."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict

from fastapi import WebSocket, WebSocketDisconnect

from src.core.env_shims import getenv_with_compat as _getenv

from src.core.orchestration.event_bus import EventBus
from src.server.event_delivery import enqueue_with_drop_policy
from src.server.event_subscriptions import resolve_initial_websocket_events
from src.server.metrics import record_dropped_session_event
from src.server.server_config import extract_admin_token_from_headers
from src.server.websocket_control import (
    build_control_error_payload,
    build_control_pong_payload,
    build_control_subscribed_payload,
    build_control_subscriptions_payload,
    build_control_unsubscribed_payload,
    parse_websocket_control_message,
)

logger = logging.getLogger(__name__)


def _make_websocket_handler(
    event_bus: EventBus,
    session_id: str,
    q: asyncio.Queue[object | None],
    drop_policy: str,
    loop: asyncio.AbstractEventLoop,
) -> Callable[[str], Callable[[dict[str, Any]], None]]:
    """Factory: build the per-event handler closure for a WebSocket connection.

    The returned handler filters by session_id, then enqueues to the websocket
    sender queue using the configured drop policy.
    """

    def _record_dropped(event_name: str) -> None:
        record_dropped_session_event(event_name, session_id)

    def make_handler(ev_name: str) -> Callable[[dict[str, Any]], None]:
        def handler(payload: dict[str, Any]) -> None:
            try:
                if isinstance(payload, dict) and "session_id" in payload:
                    if session_id != "all" and payload.get("session_id") != session_id:
                        return

                def _enqueue() -> None:
                    enqueue_with_drop_policy(
                        q,
                        (ev_name, payload),
                        drop_policy=drop_policy,
                        on_drop=_record_dropped,
                    )

                loop.call_soon_threadsafe(_enqueue)
            except Exception:
                pass

        return handler

    return make_handler


async def websocket_session_handler(
    session_id: str,
    websocket: WebSocket,
    event_bus: EventBus,
) -> None:
    """WebSocket endpoint forwarding selected EventBus events to the client.

    Features:
    - initial subscription via `events` query param (comma-separated)
    - per-connection queue size via `queue_max_size` query param
    - backpressure policy via `drop_policy` query param (drop_oldest|drop_new)
    - dynamic subscribe/unsubscribe via JSON control messages
      * {"type": "subscribe", "event": "event.name"}
      * {"type": "unsubscribe", "event": "event.name"}
    - keepalive messages controlled by `keepalive` query param (seconds)

    Authentication mirrors the HTTP admin endpoints: if CODING_AGENT_ADMIN_TOKEN is set,
    the client must provide either a Bearer token in the Authorization header or
    X-CodingAgent-Token header. Tokens passed via the query string are not accepted.
    """
    admin_token = _getenv("CODINGAGENT_ADMIN_TOKEN", "CODING_AGENT_ADMIN_TOKEN")

    qp = websocket.query_params
    events_param = qp.get("events")
    queue_max_raw = qp.get("queue_max_size") or _getenv("CODINGAGENT_SSE_QUEUE_MAX", "CODING_AGENT_SSE_QUEUE_MAX")
    try:
        qms = int(queue_max_raw) if queue_max_raw is not None else 100
    except Exception:
        qms = 100
    drop_policy = (qp.get("drop_policy") or _getenv("CODINGAGENT_SSE_DROP_POLICY", "CODING_AGENT_SSE_DROP_POLICY") or "drop_oldest").lower()
    keepalive_raw = qp.get("keepalive") or _getenv("CODINGAGENT_SSE_KEEPALIVE", "CODING_AGENT_SSE_KEEPALIVE")
    try:
        keepalive_interval = int(keepalive_raw) if keepalive_raw is not None else 15
    except Exception:
        keepalive_interval = 15

    if admin_token:
        token = extract_admin_token_from_headers(websocket.headers)
        if not token or token != admin_token:
            try:
                await websocket.close(code=1008)
            except Exception:
                pass
            return

    if not event_bus:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    await websocket.accept()

    loop = asyncio.get_running_loop()
    q: asyncio.Queue[object | None] = asyncio.Queue(maxsize=max(1, int(qms)))

    initial_events = resolve_initial_websocket_events(events_param)

    handlers: Dict[str, Callable[[dict[str, Any]], None]] = {}

    make_handler = _make_websocket_handler(event_bus, session_id, q, drop_policy, loop)

    for ev in initial_events:
        try:
            h = make_handler(ev)
            event_bus.subscribe(ev, h)
            handlers[ev] = h
        except Exception:
            pass

    async def _keepalive_sender():
        try:
            while True:
                await asyncio.sleep(keepalive_interval)
                enqueue_with_drop_policy(
                    q,
                    ("_keepalive", {"comment": "ping"}),
                    drop_policy=drop_policy,
                    on_drop=lambda ev: record_dropped_session_event(ev, session_id),
                )
        except asyncio.CancelledError:
            return

    keepalive_task = asyncio.create_task(_keepalive_sender())

    async def _sender():
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                event_name, payload = item
                try:
                    await websocket.send_json({"event": event_name, "data": payload})
                except WebSocketDisconnect:
                    break
                except Exception:
                    continue
        except asyncio.CancelledError:
            return

    async def _receiver():
        try:
            while True:
                text = await websocket.receive_text()
                msg = parse_websocket_control_message(text)
                if msg is None:
                    continue
                typ = msg.get("type")
                if typ == "subscribe":
                    ev = msg.get("event")
                    if not ev:
                        continue
                    if ev in handlers:
                        continue
                    try:
                        h = make_handler(ev)
                        event_bus.subscribe(ev, h)
                        handlers[ev] = h
                        try:
                            q.put_nowait(build_control_subscribed_payload(ev))
                        except Exception:
                            pass
                    except Exception:
                        try:
                            q.put_nowait(build_control_error_payload(ev))
                        except Exception:
                            pass
                elif typ == "unsubscribe":
                    ev = msg.get("event")
                    if not ev:
                        continue
                    h = handlers.get(ev)
                    if h:
                        try:
                            event_bus.unsubscribe(ev, h)
                        except Exception:
                            pass
                        handlers.pop(ev, None)
                        try:
                            q.put_nowait(build_control_unsubscribed_payload(ev))
                        except Exception:
                            pass
                    else:
                        try:
                            q.put_nowait(
                                build_control_unsubscribed_payload(
                                    ev, was_subscribed=False
                                )
                            )
                        except Exception:
                            pass
                elif typ == "list":
                    try:
                        q.put_nowait(
                            build_control_subscriptions_payload(list(handlers.keys()))
                        )
                    except Exception:
                        pass
                elif typ == "ping":
                    try:
                        q.put_nowait(build_control_pong_payload())
                    except Exception:
                        pass
                else:
                    pass
        except WebSocketDisconnect:
            return
        except asyncio.CancelledError:
            return
        except Exception:
            return

    sender_task = asyncio.create_task(_sender())
    receiver_task = asyncio.create_task(_receiver())

    try:
        done, pending = await asyncio.wait(
            [sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            try:
                t.cancel()
            except Exception:
                pass
    except Exception:
        logger.exception("WebSocket session error")
    finally:
        try:
            keepalive_task.cancel()
        except Exception:
            pass
        try:
            sender_task.cancel()
        except Exception:
            pass
        try:
            receiver_task.cancel()
        except Exception:
            pass
        for ev, h in list(handlers.items()):
            try:
                event_bus.unsubscribe(ev, h)
            except Exception:
                pass
