"""Lightweight scheduler/heartbeat worker.

This module provides a simple scheduler that runs periodic jobs in a
background thread and publishes events to the orchestrator EventBus. It is
designed to be minimal and robust: failures in scheduled jobs are logged and do
not crash the scheduler.

API:
- start_scheduler(orch, interval_seconds=60) -> starts background thread
- stop_scheduler() -> stops background thread
- register_job(name, fn, interval_seconds) -> register a recurring job
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_FACTORIES: Dict[str, Dict[str, Any]] = {}
_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
# B3: Lock guards _JOBS and _JOB_FACTORIES against concurrent mutation from
# the scheduler loop thread and the registration API (called from any thread).
_JOBS_LOCK = threading.Lock()


def register_job(name: str, fn: Callable[[], None], interval_seconds: int) -> None:
    """Register a recurring job.

    The job function receives no arguments; it must handle its own errors.
    """
    with _JOBS_LOCK:
        _JOBS[name] = {"fn": fn, "interval": interval_seconds, "last_run": 0}


def list_jobs() -> Dict[str, Dict[str, Any]]:
    """Return a shallow copy of registered jobs with metadata.

    The returned dict maps job name -> {"interval": int, "last_run": float}.
    """
    with _JOBS_LOCK:
        return {
            name: {"interval": job.get("interval"), "last_run": job.get("last_run")}
            for name, job in _JOBS.items()
        }


def list_job_factories() -> Dict[str, Dict[str, Any]]:
    """Return a shallow copy of registered job factories (name -> interval)."""
    with _JOBS_LOCK:
        return {
            name: {"interval": fact.get("interval")}
            for name, fact in _JOB_FACTORIES.items()
        }


def unregister_job(name: str) -> bool:
    """Unregister a job by name. Returns True if a job was removed."""
    try:
        with _JOBS_LOCK:
            return _JOBS.pop(name, None) is not None
    except Exception:
        return False


def clear_jobs() -> None:
    """Clear all registered jobs. This does not stop the scheduler thread."""
    with _JOBS_LOCK:
        _JOBS.clear()


def register_job_factory(
    name: str, fn: Callable[[], None], interval_seconds: int
) -> None:
    """Register a persistent job factory and register the job if not present.

    The factory is stored so management APIs can re-register the job when
    enabling it after a disable. This avoids losing the callable when the job
    is unregistered.
    """
    with _JOBS_LOCK:
        _JOB_FACTORIES[name] = {"fn": fn, "interval": interval_seconds}
        if name not in _JOBS:
            _JOBS[name] = {"fn": fn, "interval": interval_seconds, "last_run": 0}


def enable_job(name: str) -> bool:
    """Enable (register) a job previously registered via register_job_factory.

    Returns True if the job was registered (or already present), False if no
    factory for the job exists.
    """
    with _JOBS_LOCK:
        if name in _JOBS:
            return True
        fact = _JOB_FACTORIES.get(name)
        if not fact:
            return False
        _JOBS[name] = {"fn": fact["fn"], "interval": fact.get("interval", 0), "last_run": 0}
        return True


def disable_job(name: str) -> bool:
    """Disable (unregister) a job by name. Factory remains so it can be re-enabled."""
    return unregister_job(name)


def update_job_interval(name: str, interval_seconds: int) -> bool:
    """Update the interval for a job and/or its factory.

    Returns True if the job or factory existed and was updated.
    """
    updated = False
    with _JOBS_LOCK:
        if name in _JOBS:
            try:
                _JOBS[name]["interval"] = int(interval_seconds)
                updated = True
            except Exception:
                pass
        if name in _JOB_FACTORIES:
            try:
                _JOB_FACTORIES[name]["interval"] = int(interval_seconds)
                updated = True
            except Exception:
                pass
    return updated


def _scheduler_loop(orch, heartbeat_interval: int) -> None:
    # Determine event bus: allow passing either an Orchestrator-like object
    # (with .event_bus) or an EventBus instance directly.
    try:
        if hasattr(orch, "publish") and callable(getattr(orch, "publish")):
            _eb = orch
        else:
            _eb = getattr(orch, "event_bus", None)
            if _eb is None:
                # Fallback: try to treat orch itself as the event bus
                _eb = orch
    except Exception:
        _eb = orch

    # Publish an initial heartbeat immediately
    try:
        _eb.publish("scheduler.heartbeat", {"status": "started"})
    except Exception:
        pass

    while not _STOP_EVENT.wait(timeout=heartbeat_interval):
        now = time.time()
        # Run due jobs — take a snapshot under the lock so mutations from
        # register/unregister don't cause dict-changed-during-iteration errors.
        with _JOBS_LOCK:
            _jobs_snapshot = list(_JOBS.items())
        for name, job in _jobs_snapshot:
            try:
                last = job.get("last_run", 0)
                interval = job.get("interval", heartbeat_interval)
                if now - last >= interval:
                    success = True
                    try:
                        job["fn"]()
                    except Exception as exc:
                        # Log but don't raise — scheduler must be resilient
                        traceback.print_exc()
                        success = False
                        # Publish job failure for observability when possible
                        try:
                            _eb.publish(
                                "scheduler.job_failed",
                                {"name": name, "time": now, "error": str(exc)},
                            )
                        except Exception:
                            pass
                    finally:
                        job["last_run"] = now
                        # Publish a job_run event (success/failure) for observability
                        try:
                            _eb.publish(
                                "scheduler.job_run",
                                {"name": name, "time": now, "success": success},
                            )
                        except Exception:
                            pass
            except Exception:
                traceback.print_exc()
                # Publish heartbeat for observability
                try:
                    _eb.publish("scheduler.heartbeat", {"time": now})
                except Exception:
                    pass


def start_scheduler(orch, heartbeat_interval: int = 60) -> None:
    """Start the scheduler background thread.

    Safe to call multiple times; starting twice is a no-op.
    """
    global _THREAD, _STOP_EVENT
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(
        target=_scheduler_loop, args=(orch, heartbeat_interval), daemon=True
    )
    _THREAD.start()


def stop_scheduler() -> None:
    """Stop the scheduler background thread.

    Best-effort; may block until the current sleep finishes.
    """
    global _THREAD, _STOP_EVENT
    _STOP_EVENT.set()
    if _THREAD is not None:
        _THREAD.join(timeout=5)
        _THREAD = None
