"""Helpers shared by scheduler HTTP endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


def require_scheduler_admin_auth(request: Request, *, require_admin_auth, logger) -> None:
    """Run scheduler admin auth with consistent error mapping."""
    try:
        require_admin_auth(request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Auth check failed: {exc}")
        raise HTTPException(status_code=503, detail="Auth subsystem error")


def load_scheduler_worker(*, logger) -> Any:
    """Import the scheduler worker with consistent error mapping."""
    try:
        from src.core.scheduler import worker as sched_worker

        return sched_worker
    except Exception as exc:
        logger.warning(f"Failed to import scheduler worker: {exc}")
        raise HTTPException(status_code=503, detail="Scheduler unavailable")
