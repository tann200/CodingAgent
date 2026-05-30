"""Task execution endpoints, models, and in-process registry (G8)."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# In-process task registry: task_id -> dict with status/result/cancel_event
_TASK_REGISTRY: Dict[str, Dict[str, Any]] = {}
_TASK_REGISTRY_LOCK = threading.Lock()

# P3-2: TTL for completed/failed/cancelled task records (1 hour).
_TASK_REGISTRY_TTL_SECONDS: float = float(
    os.environ.get("CODING_AGENT_TASK_REGISTRY_TTL", "3600")
)

# Bounded thread pool for task execution — prevents unbounded thread creation.
_TASK_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.environ.get("CODING_AGENT_TASK_MAX_WORKERS", "10")),
    thread_name_prefix="task",
)


def _evict_stale_tasks() -> None:
    """Remove terminal task records older than _TASK_REGISTRY_TTL_SECONDS.

    Called inside the registry lock before each new task insertion so the
    dict stays bounded without a background thread.
    """
    cutoff = time.time() - _TASK_REGISTRY_TTL_SECONDS
    stale = [
        tid
        for tid, rec in _TASK_REGISTRY.items()
        if rec.get("status") in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        and isinstance(rec.get("finished_at"), float)
        and rec["finished_at"] < cutoff
    ]
    for tid in stale:
        _TASK_REGISTRY.pop(tid, None)


@dataclass
class _TaskEndpointDeps:
    event_bus: Optional[Any] = None
    logger: Optional[logging.Logger] = None
    require_admin_auth: Optional[Callable[[Request], None]] = None


_deps = _TaskEndpointDeps()


def register_task_endpoints(
    *,
    event_bus: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
    require_admin_auth: Optional[Callable[[Request], None]] = None,
) -> None:
    if event_bus is not None:
        _deps.event_bus = event_bus
    if logger is not None:
        _deps.logger = logger
    if require_admin_auth is not None:
        _deps.require_admin_auth = require_admin_auth


class TaskStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreateRequest(BaseModel):
    # P2-1: Enforce non-empty task with a reasonable upper bound (32 KB) to
    # prevent OOM / context-overflow from arbitrarily large inputs.
    task: str = Field(..., min_length=1, max_length=32_000)
    session_id: Optional[str] = None
    # P1-2: working_dir is validated to be an existing absolute directory so
    # callers cannot supply path-traversal strings like "../../etc".
    working_dir: Optional[str] = None
    model: Optional[str] = None

    @field_validator("working_dir", mode="before")
    @classmethod
    def _validate_working_dir(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            resolved = Path(v).resolve()
        except Exception as exc:
            raise ValueError(f"working_dir is not a valid path: {exc}") from exc
        if not resolved.is_dir():
            raise ValueError(
                f"working_dir does not exist or is not a directory: {resolved}"
            )
        # Prevent traversal outside the filesystem root — resolved path is
        # always absolute after .resolve(), so just return the canonical form.
        return str(resolved)


class TaskStatusResponse(BaseModel):
    task_id: str
    session_id: str
    status: TaskStatus
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


def _get_or_create_orchestrator(working_dir: Optional[str], model: Optional[str]):
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator(working_dir=working_dir) if working_dir else Orchestrator()
    if model:
        setattr(orch, "model", model)
    return orch


def _run_task_thread(
    task_id: str,
    session_id: str,
    task: str,
    working_dir: Optional[str],
    model: Optional[str],
    cancel_event: threading.Event,
) -> None:
    bus = _deps.event_bus
    log = _deps.logger or logging.getLogger(__name__)

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
        from src.server.app import _get_or_create_orchestrator as orchestrator_factory

        orch = orchestrator_factory(working_dir, model)
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
        log.exception("Task %s failed: %s", task_id, exc)

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


task_router = APIRouter(tags=["tasks"])


@task_router.post("/task", response_model=TaskStatusResponse, status_code=202)
async def submit_task(request_body: TaskCreateRequest, request: Request):
    if _deps.require_admin_auth is not None:
        _deps.require_admin_auth(request)
    task_id = str(uuid.uuid4())
    session_id = request_body.session_id or f"session-{task_id[:8]}"
    cancel_event = threading.Event()

    rec: Dict[str, object] = {
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
        _evict_stale_tasks()
        _TASK_REGISTRY[task_id] = rec

    bus = _deps.event_bus
    if bus:
        try:
            bus.publish(
                "session.created",
                {"session_id": session_id, "task_id": task_id},
            )
        except Exception:
            pass

    _TASK_EXECUTOR.submit(
        _run_task_thread,
        task_id,
        session_id,
        request_body.task,
        request_body.working_dir,
        request_body.model,
        cancel_event,
    )

    return TaskStatusResponse(
        task_id=task_id,
        session_id=session_id,
        status=TaskStatus.ACCEPTED,
    )


@task_router.get("/task/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str, request: Request):
    if _deps.require_admin_auth is not None:
        _deps.require_admin_auth(request)
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


@task_router.post("/task/{task_id}/cancel", response_model=TaskStatusResponse)
async def cancel_task(task_id: str, request: Request):
    if _deps.require_admin_auth is not None:
        _deps.require_admin_auth(request)
    with _TASK_REGISTRY_LOCK:
        rec = _TASK_REGISTRY.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Task not found")
    cancel_event = rec.get("cancel_event")
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


@task_router.get("/tasks", response_model=List[TaskStatusResponse])
async def list_tasks(request: Request, limit: int = 50):
    if _deps.require_admin_auth is not None:
        _deps.require_admin_auth(request)
    # A4: Cap limit to prevent a caller from dumping the entire registry in one
    # response.  Values outside [1, 500] are silently clamped.
    limit = max(1, min(limit, 500))
    with _TASK_REGISTRY_LOCK:
        items = list(_TASK_REGISTRY.values())
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
