"""HTTP/SSE server for multi-client architecture (Gap 2 implementation)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import AsyncGenerator, Dict, Optional

import uvicorn
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from contextlib import asynccontextmanager
from pydantic import BaseModel

from src.core.orchestration.event_bus import EventBus

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
        # Configurable per-client queue max size and keepalive interval
        self.queue_max_size = int(queue_max_size or 100)
        self.keepalive_interval = int(keepalive_interval or 15)
        # Backpressure policy: 'drop_oldest' (default) or 'drop_new'
        self.drop_policy = (drop_policy or "drop_oldest").lower()
        # Map of session_id -> list of (event_name, handler) tuples for cleanup
        self._session_handlers: Dict[str, list] = {}

    async def event_generator(self, session_id: str) -> AsyncGenerator[str, None]:
        """Generate SSE events for a given session."""
        # Queue to hold events for this session. Bounded to avoid unbounded
        # memory growth if a client is slow to read. Drop-oldest policy on overflow.
        event_queue: asyncio.Queue[Optional[tuple]] = asyncio.Queue(
            maxsize=self.queue_max_size
        )

        def make_handler(event_name: str):
            """Create a handler for a specific event type."""

            def handler(payload: dict) -> None:
                """Forward matching events to the queue."""
                # Add session_id to payload if not present for filtering
                if "session_id" not in payload:
                    payload = {**payload, "session_id": "unknown"}

                # Filter events by session_id if present in metadata
                if session_id == "all" or payload.get("session_id") == session_id:
                    # Put the event in the queue (non-blocking)
                    try:
                        event_queue.put_nowait((event_name, payload))
                    except asyncio.QueueFull:
                        # Handle according to configured backpressure policy
                        policy = getattr(self, "drop_policy", "drop_oldest")
                        if policy == "drop_oldest":
                            # Evict oldest item, count it as dropped, then try to enqueue
                            try:
                                dropped = event_queue.get_nowait()
                                if dropped:
                                    try:
                                        dropped_name = dropped[0]
                                        _inc_event_dropped_counter(dropped_name)
                                        _inc_client_event_dropped_counter(
                                            dropped_name, session_id
                                        )
                                    except Exception:
                                        pass
                            except Exception:
                                # nothing to evict
                                pass
                            try:
                                event_queue.put_nowait((event_name, payload))
                            except asyncio.QueueFull:
                                # Still failing: drop the new event and account for it
                                logger.warning(
                                    "Event queue full for session %s, dropping event %s",
                                    session_id,
                                    event_name,
                                )
                                try:
                                    _inc_event_dropped_counter(event_name)
                                    _inc_client_event_dropped_counter(
                                        event_name, session_id
                                    )
                                except Exception:
                                    pass
                        elif policy == "drop_new":
                            # Drop the incoming event immediately
                            logger.warning(
                                "Event queue full for session %s, dropping new event %s",
                                session_id,
                                event_name,
                            )
                            try:
                                _inc_event_dropped_counter(event_name)
                                _inc_client_event_dropped_counter(
                                    event_name, session_id
                                )
                            except Exception:
                                pass
                        else:
                            # Unknown policy: behave like drop_new
                            logger.warning(
                                "Unknown drop policy '%s' — dropping new event %s for session %s",
                                policy,
                                event_name,
                                session_id,
                            )
                            try:
                                _inc_event_dropped_counter(event_name)
                                _inc_client_event_dropped_counter(
                                    event_name, session_id
                                )
                            except Exception:
                                pass

            return handler

        # Subscribe to all event types we care about
        # Since we don't have a way to subscribe to all events, we'll subscribe to known types
        # In a production system, we might modify EventBus to support wildcards or have a separate mechanism
        key_event_types = [
            "agent.start",
            "agent.end",
            "tool.start",
            "tool.end",
            "mcp.server.status",
            "workflow.step",
            "llm.response",
            "session.created",
            "session.updated",
            # Relay perception corrective prompts to clients
            "perception.corrective_prompt",
            "error",
            "log",
        ]

        handlers = []
        for event_type in key_event_types:
            handler = make_handler(event_type)
            self.event_bus.subscribe(event_type, handler)
            handlers.append((event_type, handler))

        # Track handlers for cleanup
        if session_id not in self._session_handlers:
            self._session_handlers[session_id] = []
        self._session_handlers[session_id].extend(handlers)

        try:
            # Keepalive task to send periodic comments so intermediaries don't close idle connections
            async def _keepalive_sender():
                try:
                    while True:
                        await asyncio.sleep(self.keepalive_interval)
                        try:
                            event_queue.put_nowait(("_keepalive", {"comment": "ping"}))
                        except asyncio.QueueFull:
                            # If queue is full, drop oldest then try once
                            try:
                                _ = event_queue.get_nowait()
                            except Exception:
                                pass
                            try:
                                event_queue.put_nowait(
                                    ("_keepalive", {"comment": "ping"})
                                )
                            except Exception:
                                # Increment drop counters when keepalive can't be enqueued
                                try:
                                    _inc_event_dropped_counter("_keepalive")
                                    _inc_client_event_dropped_counter(
                                        "_keepalive", session_id
                                    )
                                except Exception:
                                    pass
                                pass
                except asyncio.CancelledError:
                    return

            keepalive_task = asyncio.create_task(_keepalive_sender())
            try:
                while True:
                    # Wait for next event
                    result = await event_queue.get()
                    if result is None:  # Sentinel to stop
                        break
                    event_name, payload = result
                    # Format as SSE
                    data = json.dumps(
                        {
                            "event": event_name,
                            "data": payload,
                        }
                    )
                    yield f"data: {data}\n\n"
            finally:
                # Ensure keepalive task is cancelled when generator ends
                try:
                    keepalive_task.cancel()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error in SSE event generator for session {session_id}: {e}")
        finally:
            # Cleanup: unsubscribe handlers and drain queue
            if session_id in self._session_handlers:
                for event_name, handler in list(
                    self._session_handlers.get(session_id, [])
                ):
                    try:
                        self.event_bus.unsubscribe(event_name, handler)
                    except Exception:
                        # Some EventBus implementations might raise; tolerate all
                        pass
                try:
                    del self._session_handlers[session_id]
                except Exception:
                    pass
            # Drain remaining items to help GC
            try:
                while not event_queue.empty():
                    _ = event_queue.get_nowait()
            except Exception:
                pass


# Pydantic models for API
class SessionCreateRequest(BaseModel):
    session_id: Optional[str] = None
    metadata: Optional[Dict[str, str]] = None


class SessionResponse(BaseModel):
    session_id: str
    status: str = "created"


# Create FastAPI app
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Lifespan handler to initialize and cleanup server resources."""
    # Create local EventBus and adapter when running as standalone server
    local_bus = EventBus()
    register_event_bus(local_bus)
    logger.info("CodingAgent HTTP/SSE server started (lifespan)")
    try:
        yield
    finally:
        logger.info("CodingAgent HTTP/SSE server shutting down (lifespan)")


app = FastAPI(title="CodingAgent Server", version="0.1.0", lifespan=_lifespan)
# Global instances (in practice, these would be managed by the orchestrator)
event_bus: Optional[EventBus] = None
sse_adapter: Optional[ServerEventBusAdapter] = None

# Simple in-process metrics (Prometheus-style text exposition without external deps)
_METRICS_LOCK = threading.Lock()
# counters keyed by (reason, model_tier) -> int
_CORRECTIVE_PROMPT_COUNTERS: Dict[tuple, int] = {}
_DROPPED_EVENT_COUNTERS: Dict[str, int] = {}
# per-client dropped counters keyed by (session_id, event_name) -> int
_CLIENT_DROPPED_EVENT_COUNTERS: Dict[tuple, int] = {}


def _inc_corrective_prompt_counter(reason: str | None, model_tier: str | None) -> None:
    key = (reason or "unknown", model_tier or "unknown")
    with _METRICS_LOCK:
        _CORRECTIVE_PROMPT_COUNTERS[key] = _CORRECTIVE_PROMPT_COUNTERS.get(key, 0) + 1


def _format_metrics_text() -> str:
    lines = [
        "# HELP codingagent_corrective_prompts_total Total corrective prompts issued",
        "# TYPE codingagent_corrective_prompts_total counter",
    ]
    total = 0
    with _METRICS_LOCK:
        items = list(_CORRECTIVE_PROMPT_COUNTERS.items())
    for (reason, tier), val in items:
        total += val
        # Escape quotes in label values
        reason_s = str(reason).replace('"', '\\"')
        tier_s = str(tier).replace('"', '\\"')
        lines.append(
            f'codingagent_corrective_prompts_total{{reason="{reason_s}",model_tier="{tier_s}"}} {val}'
        )
    lines.append(f"codingagent_corrective_prompts_total {total}")
    # Dropped events metric
    lines.append("")
    lines.append(
        "# HELP codingagent_sse_events_dropped_total Total SSE events dropped per event type"
    )
    lines.append("# TYPE codingagent_sse_events_dropped_total counter")
    with _METRICS_LOCK:
        dropped_items = list(_DROPPED_EVENT_COUNTERS.items())
    dropped_total = 0
    for ename, val in dropped_items:
        dropped_total += val
        ename_s = str(ename).replace('"', '\\"')
        lines.append(f'codingagent_sse_events_dropped_total{{event="{ename_s}"}} {val}')
    lines.append(f"codingagent_sse_events_dropped_total {dropped_total}")
    # Per-client dropped events
    lines.append("")
    lines.append(
        "# HELP codingagent_sse_events_dropped_per_client_total Total SSE events dropped per client session and event"
    )
    lines.append("# TYPE codingagent_sse_events_dropped_per_client_total counter")
    with _METRICS_LOCK:
        client_items = list(_CLIENT_DROPPED_EVENT_COUNTERS.items())
    for (sid, ename), val in client_items:
        sid_s = str(sid).replace('"', '\\"')
        ename_s = str(ename).replace('"', '\\"')
        lines.append(
            f'codingagent_sse_events_dropped_per_client_total{{session_id="{sid_s}",event="{ename_s}"}} {val}'
        )
    return "\n".join(lines) + "\n"


def _inc_event_dropped_counter(event_name: str) -> None:
    with _METRICS_LOCK:
        _DROPPED_EVENT_COUNTERS[event_name] = (
            _DROPPED_EVENT_COUNTERS.get(event_name, 0) + 1
        )


def _inc_client_event_dropped_counter(event_name: str, session_id: str) -> None:
    """Increment per-client dropped-event counter.

    session_id may be None/empty; we normalise to 'unknown'.
    """
    key = (str(session_id or "unknown"), str(event_name or "unknown"))
    with _METRICS_LOCK:
        _CLIENT_DROPPED_EVENT_COUNTERS[key] = (
            _CLIENT_DROPPED_EVENT_COUNTERS.get(key, 0) + 1
        )


def register_event_bus(bus: EventBus) -> None:
    """Bind a provided EventBus to the server and register internal subscribers.

    Use this when the orchestrator supplies the EventBus instance so server
    can reuse it rather than creating its own.
    """
    global event_bus, sse_adapter
    event_bus = bus
    # Pick SSE adapter settings from environment so deployers can tune
    # queue sizing and keepalive without changing code.
    try:
        _qms = int(os.getenv("CODING_AGENT_SSE_QUEUE_MAX", "100"))
    except Exception:
        _qms = 100
    try:
        _ka = int(os.getenv("CODING_AGENT_SSE_KEEPALIVE", "15"))
    except Exception:
        _ka = 15
    # Allow deployers to choose drop policy via env var
    _dp = os.getenv("CODING_AGENT_SSE_DROP_POLICY", "drop_oldest").lower()
    sse_adapter = ServerEventBusAdapter(
        event_bus, queue_max_size=_qms, keepalive_interval=_ka, drop_policy=_dp
    )

    # Subscribe to corrective prompt events to maintain metrics
    try:
        event_bus.subscribe("perception.corrective_prompt", _on_perception_corrective)
    except Exception:
        pass


def _on_perception_corrective(payload: Dict) -> None:
    try:
        reason = payload.get("reason") if isinstance(payload, dict) else None
        tier = payload.get("model_tier") if isinstance(payload, dict) else None
        _inc_corrective_prompt_counter(reason, tier)
    except Exception:
        pass


"""Lifespan-managed startup/shutdown handled in _lifespan."""


@app.post("/session", response_model=SessionResponse)
async def create_session(request: SessionCreateRequest):
    """Create a new session (or use provided session ID)."""
    session_id = request.session_id or f"session-{id(request)}"
    # In a full implementation, we'd store session metadata
    # For now, just publish a session created event
    if event_bus:
        event_bus.publish(
            "session.created",
            {"session_id": session_id, "metadata": request.metadata or {}},
        )
    return SessionResponse(session_id=session_id)


@app.get("/session/{session_id}/events")
async def session_events(session_id: str, request: Request):
    """Server-Sent Events endpoint for a session."""
    if not sse_adapter:
        raise HTTPException(status_code=503, detail="Server not properly initialized")

    # Return SSE response
    return StreamingResponse(
        sse_adapter.event_generator(session_id),
        media_type="text/event-stream",
    )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    """Expose in-process corrective-prompt metrics in Prometheus text format.

    This is intentionally lightweight and synchronous because the underlying
    counters are thread-safe via a lock.
    """
    # Allow optional basic auth via environment variable (single username:password)
    auth = os.getenv("CODING_AGENT_METRICS_AUTH")
    if auth:
        # Expect auth in the form 'username:password'
        try:
            import base64

            header = request.headers.get("Authorization")
            if not header or not header.startswith("Basic "):
                return Response(status_code=401, content="Unauthorized")
            b64 = header.split(" ", 1)[1]
            try:
                decoded = base64.b64decode(b64).decode("utf-8")
            except Exception:
                return Response(status_code=401, content="Unauthorized")
            if decoded != auth:
                return Response(status_code=401, content="Unauthorized")
        except Exception:
            return Response(status_code=401, content="Unauthorized")

    text = _format_metrics_text()
    return Response(content=text, media_type="text/plain; version=0.0.4")


def run_server(host: str = "127.0.0.1", port: int = 8000):
    """Run the HTTP/SSE server."""
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
