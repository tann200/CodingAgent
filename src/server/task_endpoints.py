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

from src.core.messaging.event_types import AgentEnd, AgentStart, SessionCreated

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

# Bounded queue limit for admitted tasks waiting to execute.
_TASK_MAX_QUEUE: int = int(
    os.environ.get(
        "CODINGAGENT_TASK_MAX_QUEUE",
        os.environ.get("CODING_AGENT_TASK_MAX_QUEUE", "50"),
    )
)

_ALLOWED_WORKSPACE_ROOTS: Optional[List[Path]] = None


def set_allowed_workspace_roots(roots: Optional[List[str | Path]]) -> None:
    """Explicitly set the allowed workspace roots for task execution."""
    global _ALLOWED_WORKSPACE_ROOTS
    if roots is None:
        _ALLOWED_WORKSPACE_ROOTS = None
    else:
        _ALLOWED_WORKSPACE_ROOTS = [Path(r).resolve() for r in roots]


def get_allowed_workspace_roots() -> List[Path]:
    """Return the list of allowed workspace roots.

    Defaults to CODINGAGENT_ALLOWED_WORKSPACE_ROOTS or CODING_AGENT_ALLOWED_WORKSPACE_ROOTS
    env var (delimited by os.pathsep or comma). If not configured, defaults to [Path.cwd().resolve()].
    """
    if _ALLOWED_WORKSPACE_ROOTS is not None:
        return list(_ALLOWED_WORKSPACE_ROOTS)

    env_val = os.environ.get(
        "CODINGAGENT_ALLOWED_WORKSPACE_ROOTS",
        os.environ.get("CODING_AGENT_ALLOWED_WORKSPACE_ROOTS", ""),
    ).strip()
    if env_val:
        parts: List[str] = []
        for chunk in env_val.split(os.pathsep):
            for sub in chunk.split(","):
                s = sub.strip()
                if s:
                    parts.append(s)
        if parts:
            return [Path(p).resolve() for p in parts]

    return [Path.cwd().resolve()]


def validate_working_dir_within_roots(
    working_dir: Optional[str],
    allowed_roots: Optional[List[Path]] = None,
) -> Optional[Path]:
    """Validate that *working_dir* resolves to a path within an approved workspace root.

    Raises HTTPException(403) if outside approved roots, or HTTPException(400) if invalid path.
    """
    if working_dir is None:
        return None
    try:
        resolved = Path(working_dir).resolve()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"working_dir is not a valid path: {exc}",
        ) from exc

    if not resolved.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"working_dir does not exist or is not a directory: {resolved}",
        )

    roots = allowed_roots if allowed_roots is not None else get_allowed_workspace_roots()
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise HTTPException(
            status_code=403,
            detail=f"working_dir '{resolved}' is not within an approved workspace root",
        )
    return resolved


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
        # If task was cancelled before starting, abort immediately without running or creating orchestrator
        if cancel_event.is_set() or rec.get("status") == TaskStatus.CANCELLED:
            rec["status"] = TaskStatus.CANCELLED
            rec["finished_at"] = time.time()
            if bus:
                try:
                    bus.publish_typed(
                        AgentEnd(
                            task_id=task_id,
                            session_id=session_id,
                            status=TaskStatus.CANCELLED.value,
                            result="",
                            error="Cancelled before start",
                        )
                    )
                except Exception:
                    pass
            return
        rec["status"] = TaskStatus.RUNNING
        rec["started_at"] = time.time()

    if bus:
        try:
            bus.publish_typed(AgentStart(task_id=task_id, session_id=session_id, task=task[:200]))
        except Exception:
            pass

    result_text: Optional[str] = None
    error_text: Optional[str] = None
    try:
        if cancel_event.is_set():
            raise RuntimeError("Task was cancelled before start")

        from src.server.app import _get_or_create_orchestrator as orchestrator_factory
        orch = orchestrator_factory(working_dir, model)

        # Propagate task_id and session_id into orchestrator state so session stores,
        # message history, and checkpoints are tied to the client session.
        orch._current_task_id = session_id
        orch.session_id = session_id
        orch.task_id = task_id
        if hasattr(orch, "session_mgr") and orch.session_mgr:
            try:
                orch.session_mgr.task_id = session_id
                if hasattr(orch.session_mgr, "update_session_state"):
                    orch.session_mgr.update_session_state(session_id=session_id)
            except Exception:
                pass

        if bus:
            try:
                orch.event_bus = bus
            except Exception:
                pass

        if cancel_event.is_set():
            raise RuntimeError("Task was cancelled before start")

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
            bus.publish_typed(AgentEnd(task_id=task_id, session_id=session_id, status=final_status.value, result=(result_text or "")[:500], error=error_text))
        except Exception:
            pass


task_router = APIRouter(tags=["tasks"])


@task_router.post("/task", response_model=TaskStatusResponse, status_code=202)
async def submit_task(request_body: TaskCreateRequest, request: Request):
    if _deps.require_admin_auth is not None:
        _deps.require_admin_auth(request)

    # Enforce workspace root allowlist
    resolved_workdir = validate_working_dir_within_roots(request_body.working_dir)
    working_dir_str = str(resolved_workdir) if resolved_workdir else None

    task_id = str(uuid.uuid4())
    session_id = request_body.session_id or f"session-{task_id[:8]}"
    cancel_event = threading.Event()

    with _TASK_REGISTRY_LOCK:
        _evict_stale_tasks()
        # Enforce bounded queue capacity for waiting/accepted tasks
        queued_count = sum(
            1
            for rec in _TASK_REGISTRY.values()
            if rec.get("status") == TaskStatus.ACCEPTED
        )
        if queued_count >= _TASK_MAX_QUEUE:
            raise HTTPException(
                status_code=503,
                detail="Task queue is full; server overloaded. Please retry later.",
            )

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
        _TASK_REGISTRY[task_id] = rec

    bus = _deps.event_bus
    if bus:
        try:
            bus.publish_typed(SessionCreated(session_id=session_id, task_id=task_id))
        except Exception:
            pass

    _TASK_EXECUTOR.submit(
        _run_task_thread,
        task_id,
        session_id,
        request_body.task,
        working_dir_str,
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
        # If task was still ACCEPTED (queued), mark CANCELLED immediately
        if rec.get("status") == TaskStatus.ACCEPTED:
            rec["status"] = TaskStatus.CANCELLED
            rec["finished_at"] = time.time()
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
