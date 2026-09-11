"""HTTP/SSE server for multi-client architecture (Gap 2 implementation).

Deployment notes
----------------
See ``server_config.py`` for the full deployment guide including TLS,
reverse-proxy, and accepted auth headers.

Endpoint auth policy (summary):
  GET /health               — public liveness; returns {"status": "healthy"} only
  GET /health/details       — admin auth; full capability data
  GET /metrics              — metrics Basic-auth if configured, else admin auth
  POST /session             — admin auth
  GET  /session/{id}/events — admin auth (SSE)
  WS   /ws/session/{id}     — admin auth (WebSocket; checked in websocket_handler)
  POST /task, GET /task/…   — admin auth
  GET|POST /scheduler/…     — admin auth
"""

from __future__ import annotations

import hmac
import logging
import threading
import uuid
from typing import Dict, Optional

import uvicorn

from src.core.env_shims import getenv_with_compat as _getenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse, Response
from contextlib import asynccontextmanager
from pydantic import BaseModel

from src.core.orchestration.event_bus import EventBus
from src.core.messaging.event_types import SessionCreated

from src.server.websocket_handler import websocket_session_handler
from src.server import task_endpoints as _task_endpoints
from src.server.server_config import (
    extract_admin_token_from_headers,
    metrics_basic_auth_valid,
    read_sse_adapter_settings,
    validate_server_exposure,
)
from src.server.scheduler_endpoints import (
    load_scheduler_worker,
    require_scheduler_admin_auth,
)
from src.server.metrics import (
    format_metrics_text,
    inc_admin_auth_counter,
    inc_corrective_prompt_counter,
)
from src.server.sse_adapter import ServerEventBusAdapter

_TASK_REGISTRY = _task_endpoints._TASK_REGISTRY
_TASK_REGISTRY_LOCK = _task_endpoints._TASK_REGISTRY_LOCK
_get_or_create_orchestrator = _task_endpoints._get_or_create_orchestrator
register_task_endpoints = _task_endpoints.register_task_endpoints
task_router = _task_endpoints.task_router

logger = logging.getLogger(__name__)


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
app.include_router(task_router)
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
        register_task_endpoints(event_bus=event_bus)
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
    """Authorize a protected HTTP request.

    Accepts Bearer token via Authorization header or X-CodingAgent-Token header.

    Fail-closed policy:
    - If ``CODINGAGENT_ADMIN_TOKEN`` is configured, a matching token is required.
    - If NO admin token is configured, the request is only allowed from a
      loopback client.  Non-loopback clients are rejected (403) even under a
      direct-uvicorn launch that skipped ``validate_server_exposure``.  This keeps
      local development friction-free while failing closed for remote exposure.
    """
    admin_token = _getenv("CODINGAGENT_ADMIN_TOKEN", "CODING_AGENT_ADMIN_TOKEN")
    if not admin_token:
        # No token configured — permit loopback only.
        if loopback_only_allowed(request, admin_token):
            return
        raise HTTPException(
            status_code=403,
            detail="Non-loopback access requires CODINGAGENT_ADMIN_TOKEN",
        )
    inc_admin_auth_counter("attempts")
    token = extract_admin_token_from_headers(request.headers)
    if token and hmac.compare_digest(token, admin_token):
        inc_admin_auth_counter("successes")
        return
    inc_admin_auth_counter("failures")
    raise HTTPException(status_code=401, detail="Unauthorized")


register_task_endpoints(logger=logger, require_admin_auth=_require_admin_auth)


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
    session_id = session_req.session_id or f"session-{uuid.uuid4().hex}"
    # In a full implementation, we'd store session metadata
    # For now, just publish a session created event
    if event_bus:
        event_bus.publish_typed(SessionCreated(session_id=session_id, metadata=session_req.metadata or {}))
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
    """Public liveness probe.

    Returns only minimal status so liveness checks work without credentials.
    Capability details are available at ``GET /health/details`` (admin auth required).
    """
    return {"status": "healthy"}


@app.get("/health/details")
async def health_details(request: Request):
    """Admin-protected endpoint returning full capability flags.

    Operators can see which optional features are active without reading source.
    Requires admin auth on protected deployments.
    """
    _require_admin_auth(request)

    # Report semantic search mode so operators know if sentence-transformers
    # is installed and real embedding-based search is active.
    _semantic_search_available = False
    try:
        import sentence_transformers  # type: ignore[import-not-found]  # noqa: F401
        _semantic_search_available = True
    except ImportError:
        pass

    # Report OTel export status.
    import os as _os
    _otel_enabled = bool(_os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""))

    return {
        "status": "healthy",
        "capabilities": {
            "semantic_search": _semantic_search_available,
            "semantic_search_note": (
                "real embedding-based search active"
                if _semantic_search_available
                else "SHA-256 stub active — install sentence-transformers for real semantic search: pip install codingagent[semantic]"
            ),
            "otel_export": _otel_enabled,
        },
    }


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    """Expose in-process metrics in Prometheus text format.

    Auth policy (checked in order):
    1. If ``CODINGAGENT_METRICS_AUTH`` / ``CODING_AGENT_METRICS_AUTH`` is set,
       require HTTP Basic auth with those credentials.
    2. Otherwise, if ``CODINGAGENT_ADMIN_TOKEN`` is set (protected deployment),
       require the same admin Bearer/X-CodingAgent-Token auth.
    3. If neither is configured (local-only deployment), the endpoint is open.

    This ensures metrics are never public on a protected deployment.
    """
    metrics_auth = _getenv("CODINGAGENT_METRICS_AUTH", "CODING_AGENT_METRICS_AUTH")
    if metrics_auth:
        # Explicit Basic-auth credential configured — use it (constant-time check
        # is inside metrics_basic_auth_valid).
        if not metrics_basic_auth_valid(request.headers, metrics_auth):
            return Response(status_code=401, content="Unauthorized")
    else:
        # Fall back to admin auth policy so metrics are never public on a
        # token-protected deployment.
        _require_admin_auth(request)

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
    """WebSocket endpoint — delegates to websocket_handler for full auth + logic.

    Auth is handled entirely inside ``websocket_session_handler`` to keep the
    check co-located with the connection logic and avoid drift between this
    thin wrapper and the handler.
    """
    if event_bus is None:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return
    await websocket_session_handler(session_id, websocket, event_bus)


def run_server(host: str = "127.0.0.1", port: int = 8000):
    """Run the HTTP/SSE server.

    This is the supported launcher.  Running uvicorn directly bypasses the
    ``validate_server_exposure`` startup guard — set ``CODINGAGENT_ADMIN_TOKEN``
    explicitly in that case.
    """
    admin_token = _getenv("CODINGAGENT_ADMIN_TOKEN", "CODING_AGENT_ADMIN_TOKEN")
    validate_server_exposure(host, admin_token)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
