"""HTTP/SSE server for multi-client architecture (Gap 2 implementation)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from enum import Enum
from typing import AsyncGenerator, Dict, List, Optional, Any

import uvicorn
import os
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, Response
from contextlib import asynccontextmanager
from pydantic import BaseModel

from src.core.orchestration.event_bus import EventBus
from src.server.event_delivery import enqueue_with_drop_policy, record_dropped_event
from src.server.event_subscriptions import (
    DEFAULT_SERVER_EVENT_TYPES,
    resolve_initial_websocket_events,
)
from src.server.websocket_control import (
    build_control_error_payload,
    build_control_pong_payload,
    build_control_subscribed_payload,
    build_control_subscriptions_payload,
    build_control_unsubscribed_payload,
    parse_websocket_control_message,
)
from src.server.server_config import (
    extract_admin_token_from_headers,
    metrics_basic_auth_valid,
    read_sse_adapter_settings,
)
from src.server.scheduler_endpoints import (
    load_scheduler_worker,
    require_scheduler_admin_auth,
)
from src.server.metrics import (
    format_metrics_text,
    inc_admin_auth_counter,
    inc_corrective_prompt_counter,
    inc_client_event_dropped_counter,
    inc_event_dropped_counter,
    record_dropped_session_event,
)
from src.server.sse_adapter import ServerEventBusAdapter

logger = logging.getLogger(__name__)


# Pydantic models for API
class SessionCreateRequest(BaseModel):
    session_id: Optional[str] = None
    metadata: Optional[Dict[str, str]] = None


class SessionResponse(BaseModel):
    session_id: str
    status: str = "created"


# ---------------------------------------------------------------------------
# Task execution models and in-process registry
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreateRequest(BaseModel):
    task: str
    session_id: Optional[str] = None
    working_dir: Optional[str] = None
    model: Optional[str] = None


class TaskStatusResponse(BaseModel):
    task_id: str
    session_id: str
    status: TaskStatus
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


# In-process task registry: task_id -> dict with status/result/cancel_event
_TASK_REGISTRY: Dict[str, Dict[str, Any]] = {}
_TASK_REGISTRY_LOCK = threading.Lock()


def _get_or_create_orchestrator(working_dir: Optional[str], model: Optional[str]):
    """Return a lightweight Orchestrator for ad-hoc task execution.

    Import is deferred so the server stays importable without all heavy deps
    when used in tests.
    """
    from src.core.orchestration.orchestrator import Orchestrator  # type: ignore[import]

    kwargs: Dict[str, Any] = {}
    if working_dir:
        kwargs["working_dir"] = working_dir
    if model:
        kwargs["model"] = model
    return Orchestrator(**kwargs)


def _run_task_thread(
    task_id: str,
    session_id: str,
    task: str,
    working_dir: Optional[str],
    model: Optional[str],
    cancel_event: threading.Event,
    bus: Optional[EventBus],
) -> None:
    """Execute a task in a background thread and update the registry."""
    with _TASK_REGISTRY_LOCK:
        rec = _TASK_REGISTRY.get(task_id)
        if rec is None:
            return
        rec["status"] = TaskStatus.RUNNING
        rec["started_at"] = time.time()

    if bus:
        try:
            bus.publish(
                "agent.start",
                {"task_id": task_id, "session_id": session_id, "task": task[:200]},
            )
        except Exception:
            pass

    result_text: Optional[str] = None
    error_text: Optional[str] = None
    try:
        orch = _get_or_create_orchestrator(working_dir, model)
        # Wire EventBus so task events flow to SSE/WS clients
        if bus:
            try:
                orch.event_bus = bus
            except Exception:
                pass

        messages = [{"role": "user", "content": task}]
        tools = getattr(orch, "tools", {}) or {}
        try:
            tools = orch.get_tools_for_role("default")
        except Exception:
            pass

        result = orch.run_agent_once(
            system_prompt_name=None,
            messages=messages,
            tools=tools,
            cancel_event=cancel_event,
        )
        if isinstance(result, dict):
            result_text = (
                result.get("assistant_message") or result.get("result") or str(result)
            )
            if not result.get("ok", True):
                error_text = result.get("error")
        else:
            result_text = str(result)
    except Exception as exc:
        error_text = str(exc)
        logger.exception("Task %s failed: %s", task_id, exc)

    final_status = TaskStatus.FAILED if error_text else TaskStatus.COMPLETED
    if cancel_event.is_set():
        final_status = TaskStatus.CANCELLED

    with _TASK_REGISTRY_LOCK:
        rec = _TASK_REGISTRY.get(task_id, {})
        rec["status"] = final_status
        rec["result"] = result_text
        rec["error"] = error_text
        rec["finished_at"] = time.time()

    if bus:
        try:
            bus.publish(
                "agent.end",
                {
                    "task_id": task_id,
                    "session_id": session_id,
                    "status": final_status.value,
                    "result": (result_text or "")[:500],
                    "error": error_text,
                },
            )
        except Exception:
            pass


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
_REGISTERED_CORRECTIVE_BUS: Optional[EventBus] = None
_REGISTER_EVENT_BUS_LOCK = threading.Lock()

def register_event_bus(bus: EventBus) -> None:
    """Bind a provided EventBus to the server and register internal subscribers.

    Use this when the orchestrator supplies the EventBus instance so server
    can reuse it rather than creating its own.

    Thread-safe: a lock prevents concurrent calls from double-subscribing or
    creating multiple ``sse_adapter`` instances.
    """
    global event_bus, sse_adapter, _REGISTERED_CORRECTIVE_BUS
    with _REGISTER_EVENT_BUS_LOCK:
        if event_bus is bus and sse_adapter is not None:
            return

        if _REGISTERED_CORRECTIVE_BUS is not None and _REGISTERED_CORRECTIVE_BUS is not bus:
            try:
                _REGISTERED_CORRECTIVE_BUS.unsubscribe(
                    "perception.corrective_prompt", _on_perception_corrective
                )
            except Exception:
                pass

        event_bus = bus
        # Pick SSE adapter settings from environment so deployers can tune
        # queue sizing and keepalive without changing code.
        _qms, _ka, _dp = read_sse_adapter_settings()
        sse_adapter = ServerEventBusAdapter(
            event_bus, queue_max_size=_qms, keepalive_interval=_ka, drop_policy=_dp
        )

        # Subscribe to corrective prompt events to maintain metrics
        try:
            event_bus.subscribe("perception.corrective_prompt", _on_perception_corrective)
            _REGISTERED_CORRECTIVE_BUS = event_bus
        except Exception:
            pass


def _require_admin_auth(request: Request) -> None:
    """Require admin token if CODING_AGENT_ADMIN_TOKEN environment variable is set.

    Accepts Bearer token via Authorization header or X-CodingAgent-Token header.
    If no admin token is configured, the endpoints are open for local usage.
    """
    admin_token = os.getenv("CODING_AGENT_ADMIN_TOKEN")
    if not admin_token:
        return
    inc_admin_auth_counter("attempts")
    token = extract_admin_token_from_headers(request.headers)
    if token and token == admin_token:
        inc_admin_auth_counter("successes")
        return
    inc_admin_auth_counter("failures")
    raise HTTPException(status_code=401, detail="Unauthorized")


def _on_perception_corrective(payload: Dict) -> None:
    try:
        reason = payload.get("reason") if isinstance(payload, dict) else None
        tier = payload.get("model_tier") if isinstance(payload, dict) else None
        inc_corrective_prompt_counter(reason, tier)
    except Exception:
        pass


"""Lifespan-managed startup/shutdown handled in _lifespan."""


@app.post("/session", response_model=SessionResponse)
async def create_session(session_req: SessionCreateRequest, request: Request):
    """Create a new session (or use provided session ID)."""
    _require_admin_auth(request)
    session_id = session_req.session_id or f"session-{id(session_req)}"
    # In a full implementation, we'd store session metadata
    # For now, just publish a session created event
    if event_bus:
        event_bus.publish(
            "session.created",
            {"session_id": session_id, "metadata": session_req.metadata or {}},
        )
    return SessionResponse(session_id=session_id)


@app.get("/session/{session_id}/events")
async def session_events(session_id: str, request: Request):
    """Server-Sent Events endpoint for a session."""
    _require_admin_auth(request)
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
        if not metrics_basic_auth_valid(request.headers, auth):
            return Response(status_code=401, content="Unauthorized")

    text = format_metrics_text()
    return Response(content=text, media_type="text/plain; version=0.0.4")


@app.get("/scheduler/jobs")
async def list_scheduler_jobs(request: Request):
    """List registered scheduler jobs and metadata."""
    require_scheduler_admin_auth(
        request, require_admin_auth=_require_admin_auth, logger=logger
    )
    _sched = load_scheduler_worker(logger=logger)

    try:
        jobs = _sched.list_jobs()
        factories = {}
        try:
            factories = _sched.list_job_factories()
        except Exception:
            factories = {}
        return {"jobs": jobs, "factories": factories}
    except Exception as e:
        logger.warning(f"Failed to list scheduler jobs: {e}")
        raise HTTPException(status_code=503, detail="Scheduler unavailable")


@app.post("/scheduler/jobs/{name}/unregister")
async def unregister_scheduler_job(name: str, request: Request):
    """Unregister a scheduler job by name."""
    require_scheduler_admin_auth(
        request, require_admin_auth=_require_admin_auth, logger=logger
    )
    _sched = load_scheduler_worker(logger=logger)

    try:
        removed = _sched.disable_job(name)
        if not removed:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"removed": True, "name": name}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to unregister job {name}: {e}")
        raise HTTPException(status_code=503, detail="Scheduler unavailable")


@app.post("/scheduler/jobs/clear")
async def clear_scheduler_jobs(request: Request):
    """Clear all registered scheduler jobs."""
    require_scheduler_admin_auth(
        request, require_admin_auth=_require_admin_auth, logger=logger
    )
    _sched = load_scheduler_worker(logger=logger)

    try:
        _sched.clear_jobs()
        return {"cleared": True}
    except Exception as e:
        logger.warning(f"Failed to clear scheduler jobs: {e}")
        raise HTTPException(status_code=503, detail="Scheduler unavailable")


@app.post("/scheduler/jobs/{name}/enable")
async def enable_scheduler_job(name: str, request: Request):
    """Enable/restore a job registered via a job factory."""
    require_scheduler_admin_auth(
        request, require_admin_auth=_require_admin_auth, logger=logger
    )
    _sched = load_scheduler_worker(logger=logger)

    try:
        ok = _sched.enable_job(name)
        if not ok:
            raise HTTPException(status_code=404, detail="Job factory not found")
        return {"enabled": True, "name": name}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to enable job {name}: {e}")
        raise HTTPException(status_code=503, detail="Scheduler unavailable")


@app.post("/scheduler/jobs/{name}/interval")
async def update_scheduler_job_interval(name: str, request: Request):
    """Update the interval for a specific job.

    Expects JSON body: {"interval": <seconds>}.
    """
    try:
        payload = await request.json()
        interval = int(payload.get("interval", 0))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload")

    require_scheduler_admin_auth(
        request, require_admin_auth=_require_admin_auth, logger=logger
    )
    _sched = load_scheduler_worker(logger=logger)

    try:
        ok = _sched.update_job_interval(name, interval)
        if not ok:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"updated": True, "name": name, "interval": interval}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to update interval for job {name}: {e}")
        raise HTTPException(status_code=503, detail="Scheduler unavailable")


@app.websocket("/ws/session/{session_id}")
async def websocket_session_events(session_id: str, websocket: WebSocket):
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
    # Basic admin-token check using WebSocket headers (no Request object available)
    admin_token = os.getenv("CODING_AGENT_ADMIN_TOKEN")

    # Read query params early so clients can pass queue sizing and initial subs
    qp = websocket.query_params
    events_param = qp.get("events")
    try:
        qms = int(
            qp.get("queue_max_size") or os.getenv("CODING_AGENT_SSE_QUEUE_MAX", 100)
        )
    except Exception:
        qms = int(os.getenv("CODING_AGENT_SSE_QUEUE_MAX", 100))
    drop_policy = (
        qp.get("drop_policy")
        or os.getenv("CODING_AGENT_SSE_DROP_POLICY", "drop_oldest")
    ).lower()
    try:
        keepalive_interval = int(
            qp.get("keepalive") or os.getenv("CODING_AGENT_SSE_KEEPALIVE", 15)
        )
    except Exception:
        keepalive_interval = int(os.getenv("CODING_AGENT_SSE_KEEPALIVE", 15))

    if admin_token:
        token = extract_admin_token_from_headers(websocket.headers)
        # Do not accept token via query string; require header-based auth for WebSocket.
        if not token or token != admin_token:
            try:
                await websocket.close(code=1008)
            except Exception:
                pass
            return

    # Ensure event_bus has been registered
    if not event_bus:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    # Accept the websocket connection
    await websocket.accept()

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(qms)))

    # Derive initial subscription list
    initial_events = resolve_initial_websocket_events(events_param)

    # Map event_name -> handler for unsubscribe/management
    handlers: Dict[str, callable] = {}

    def _record_dropped(event_name: str) -> None:
        record_dropped_session_event(event_name, session_id)

    def make_handler(ev_name: str):
        # Handler executed in publisher thread; it schedules enqueue on the event loop
        def handler(payload: dict) -> None:
            try:
                # Filter by session_id when present in payload
                if isinstance(payload, dict) and "session_id" in payload:
                    if session_id != "all" and payload.get("session_id") != session_id:
                        return

                # enqueue on the event loop to avoid cross-thread queue access
                def _enqueue():
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

    # Register initial handlers
    for ev in initial_events:
        try:
            h = make_handler(ev)
            event_bus.subscribe(ev, h)
            handlers[ev] = h
        except Exception:
            pass

    # Keepalive task: periodically enqueue a keepalive message
    async def _keepalive_sender():
        try:
            while True:
                await asyncio.sleep(keepalive_interval)
                enqueue_with_drop_policy(
                    q,
                    ("_keepalive", {"comment": "ping"}),
                    drop_policy=drop_policy,
                    on_drop=_record_dropped,
                )
        except asyncio.CancelledError:
            return

    keepalive_task = asyncio.create_task(_keepalive_sender())

    # Sender task: forwards queue items to the websocket
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
                    # ignore send errors and continue
                    continue
        except asyncio.CancelledError:
            return

    # Receiver task: handles control messages from the client
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
                        # already subscribed
                        continue
                    try:
                        h = make_handler(ev)
                        event_bus.subscribe(ev, h)
                        handlers[ev] = h
                        # acknowledge via control envelope
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
                        # Unknown/unregistered event: still acknowledge the request
                        # to keep client control flow simple (idempotent).
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
                    # unknown control type, ignore
                    pass
        except WebSocketDisconnect:
            return
        except asyncio.CancelledError:
            return
        except Exception:
            # Any other receiver error should close the connection
            return

    sender_task = asyncio.create_task(_sender())
    receiver_task = asyncio.create_task(_receiver())

    try:
        # Wait until either task exits
        done, pending = await asyncio.wait(
            [sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED
        )
        # Cancel remaining tasks
        for t in pending:
            try:
                t.cancel()
            except Exception:
                pass
    except Exception:
        logger.exception("WebSocket session error")
    finally:
        # Cleanup: cancel keepalive and remaining tasks
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
        # Unsubscribe handlers
        for ev, h in list(handlers.items()):
            try:
                event_bus.unsubscribe(ev, h)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Task execution endpoints (G8)
# ---------------------------------------------------------------------------


@app.post("/task", response_model=TaskStatusResponse, status_code=202)
async def submit_task(request_body: TaskCreateRequest, request: Request):
    """Submit a task for asynchronous execution.

    Returns immediately with task_id and status=accepted.  The orchestrator
    runs in a background thread and publishes agent.start / agent.end events
    to the EventBus so SSE/WS clients receive live updates.

    Poll GET /task/{task_id} for the result.
    """
    _require_admin_auth(request)
    task_id = str(uuid.uuid4())
    session_id = request_body.session_id or f"session-{task_id[:8]}"
    cancel_event = threading.Event()

    rec: Dict[str, Any] = {
        "task_id": task_id,
        "session_id": session_id,
        "status": TaskStatus.ACCEPTED,
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
        "cancel_event": cancel_event,
    }
    with _TASK_REGISTRY_LOCK:
        _TASK_REGISTRY[task_id] = rec

    if event_bus:
        try:
            event_bus.publish(
                "session.created",
                {"session_id": session_id, "task_id": task_id},
            )
        except Exception:
            pass

    thread = threading.Thread(
        target=_run_task_thread,
        args=(
            task_id,
            session_id,
            request_body.task,
            request_body.working_dir,
            request_body.model,
            cancel_event,
            event_bus,
        ),
        daemon=True,
        name=f"task-{task_id[:8]}",
    )
    thread.start()

    return TaskStatusResponse(
        task_id=task_id,
        session_id=session_id,
        status=TaskStatus.ACCEPTED,
    )


@app.get("/task/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str, request: Request):
    """Return the current status and result of a submitted task."""
    _require_admin_auth(request)
    with _TASK_REGISTRY_LOCK:
        rec = _TASK_REGISTRY.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatusResponse(
        task_id=rec["task_id"],
        session_id=rec["session_id"],
        status=rec["status"],
        result=rec.get("result"),
        error=rec.get("error"),
        started_at=rec.get("started_at"),
        finished_at=rec.get("finished_at"),
    )


@app.post("/task/{task_id}/cancel", response_model=TaskStatusResponse)
async def cancel_task(task_id: str, request: Request):
    """Signal a running task to cancel.

    Sets the cancel_event that run_agent_once_impl checks between steps.
    Returns the current task record — status will transition to cancelled
    once the running step completes.
    """
    _require_admin_auth(request)
    with _TASK_REGISTRY_LOCK:
        rec = _TASK_REGISTRY.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Task not found")
    cancel_event: threading.Event = rec.get("cancel_event")
    if cancel_event:
        cancel_event.set()
    return TaskStatusResponse(
        task_id=rec["task_id"],
        session_id=rec["session_id"],
        status=rec["status"],
        result=rec.get("result"),
        error=rec.get("error"),
        started_at=rec.get("started_at"),
        finished_at=rec.get("finished_at"),
    )


@app.get("/tasks", response_model=List[TaskStatusResponse])
async def list_tasks(request: Request, limit: int = 50):
    """List recent tasks (newest first, capped at limit)."""
    _require_admin_auth(request)
    with _TASK_REGISTRY_LOCK:
        items = list(_TASK_REGISTRY.values())
    # Sort by started_at descending (None last)
    items.sort(key=lambda r: r.get("started_at") or 0.0, reverse=True)
    items = items[: max(1, limit)]
    return [
        TaskStatusResponse(
            task_id=r["task_id"],
            session_id=r["session_id"],
            status=r["status"],
            result=r.get("result"),
            error=r.get("error"),
            started_at=r.get("started_at"),
            finished_at=r.get("finished_at"),
        )
        for r in items
    ]


def run_server(host: str = "127.0.0.1", port: int = 8000):
    """Run the HTTP/SSE server."""
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
