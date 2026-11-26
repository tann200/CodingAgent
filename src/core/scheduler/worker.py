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

import atexit
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.core.security.redaction import redact_secrets

_logger = logging.getLogger(__name__)

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_FACTORIES: Dict[str, Dict[str, Any]] = {}
_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_JOBS_LOCK = threading.Lock()
_LIFECYCLE_LOCK = threading.RLock()
_JOB_EXECUTOR: Optional[ThreadPoolExecutor] = None
_STATE_PATH: Optional[Path] = None
_JOB_TIMEOUT_SECONDS: float = float(os.environ.get("CODINGAGENT_SCHEDULER_JOB_TIMEOUT", "300") or 300)


def _validate_interval(interval_seconds: int) -> int:
    val = int(interval_seconds)
    if val < 1:
        raise ValueError(f"interval_seconds must be >= 1, got {interval_seconds!r}")
    return val


def set_state_path(path: Optional[Path | str]) -> None:
    """Configure optional persistent state JSON path for scheduler job intervals/last_run."""
    global _STATE_PATH
    with _JOBS_LOCK:
        _STATE_PATH = Path(path) if path is not None else None


def _persist_state_locked() -> None:
    if _STATE_PATH is None:
        return
    try:
        from src.core.io_utils import atomic_write_json

        payload = {
            name: {
                "interval": int(job.get("interval", 60)),
                "last_run": float(job.get("last_run", 0.0)),
            }
            for name, job in _JOBS.items()
        }
        atomic_write_json(_STATE_PATH, payload, logger=_logger)
    except Exception as exc:
        _logger.warning("Scheduler state persistence failed: %s", redact_secrets(str(exc)))


def load_persisted_state() -> Dict[str, Dict[str, Any]]:
    """Restore persisted intervals/last_run timestamps for already-registered jobs."""
    with _JOBS_LOCK:
        if _STATE_PATH is None or not _STATE_PATH.exists():
            return {}
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            _logger.warning("Failed to load scheduler state: %s", redact_secrets(str(exc)))
            return {}
        if not isinstance(raw, dict):
            return {}
        for name, meta in raw.items():
            if not isinstance(meta, dict) or name not in _JOBS:
                continue
            try:
                interval_val = int(meta.get("interval", _JOBS[name]["interval"]))
                if interval_val >= 1:
                    _JOBS[name]["interval"] = interval_val
                last_run_val = float(meta.get("last_run", _JOBS[name].get("last_run", 0)))
                if last_run_val >= 0:
                    _JOBS[name]["last_run"] = last_run_val
            except Exception:
                continue
        return raw


def register_job(name: str, fn: Callable[[], None], interval_seconds: int) -> None:
    """Register a recurring job.

    The job function receives no arguments; it must handle its own errors.
    """
    interval = _validate_interval(interval_seconds)
    with _JOBS_LOCK:
        _JOBS[name] = {"fn": fn, "interval": interval, "last_run": 0}
        _persist_state_locked()


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
            removed = _JOBS.pop(name, None) is not None
            if removed:
                _persist_state_locked()
            return removed
    except Exception:
        return False


def clear_jobs() -> None:
    """Clear all registered jobs. This does not stop the scheduler thread."""
    with _JOBS_LOCK:
        _JOBS.clear()
        _persist_state_locked()


def register_job_factory(
    name: str, fn: Callable[[], None], interval_seconds: int
) -> None:
    """Register a persistent job factory and register the job if not present.

    The factory is stored so management APIs can re-register the job when
    enabling it after a disable. This avoids losing the callable when the job
    is unregistered.
    """
    interval = _validate_interval(interval_seconds)
    with _JOBS_LOCK:
        _JOB_FACTORIES[name] = {"fn": fn, "interval": interval}
        if name not in _JOBS:
            _JOBS[name] = {"fn": fn, "interval": interval, "last_run": 0}
            _persist_state_locked()


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
        interval = max(1, int(fact.get("interval", 60)))
        _JOBS[name] = {"fn": fact["fn"], "interval": interval, "last_run": 0}
        _persist_state_locked()
        return True


def disable_job(name: str) -> bool:
    """Disable (unregister) a job by name. Factory remains so it can be re-enabled."""
    return unregister_job(name)


def update_job_interval(name: str, interval_seconds: int) -> bool:
    """Update the interval for a job and/or its factory.

    Returns True if the job or factory existed and was updated.
    """
    if int(interval_seconds) < 1:
        return False
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
        if updated:
            _persist_state_locked()
    return updated


def _get_job_executor() -> ThreadPoolExecutor:
    global _JOB_EXECUTOR
    with _LIFECYCLE_LOCK:
        if _JOB_EXECUTOR is None or getattr(_JOB_EXECUTOR, "_shutdown", False):
            _JOB_EXECUTOR = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="scheduler_job",
            )
        return _JOB_EXECUTOR


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
                        timeout_s = float(job.get("timeout", _JOB_TIMEOUT_SECONDS))
                        fut = _get_job_executor().submit(job["fn"])
                        fut.result(timeout=max(0.1, timeout_s))
                    except FuturesTimeoutError:
                        success = False
                        _logger.warning("Scheduler job %r timed out", name)
                        try:
                            _eb.publish(
                                "scheduler.job_failed",
                                {"name": name, "time": now, "error": "job timed out"},
                            )
                        except Exception:
                            pass
                    except Exception as exc:
                        safe_err = redact_secrets(str(exc))
                        _logger.warning("Scheduler job %r failed: %s", name, safe_err)
                        success = False
                        try:
                            _eb.publish(
                                "scheduler.job_failed",
                                {"name": name, "time": now, "error": safe_err},
                            )
                        except Exception:
                            pass
                    finally:
                        with _JOBS_LOCK:
                            if name in _JOBS:
                                _JOBS[name]["last_run"] = now
                                _persist_state_locked()
                            else:
                                job["last_run"] = now
                        try:
                            _eb.publish(
                                "scheduler.job_run",
                                {"name": name, "time": now, "success": success},
                            )
                        except Exception:
                            pass
            except Exception as exc:
                _logger.warning("Scheduler loop error for %r: %s", name, redact_secrets(str(exc)))
                try:
                    _eb.publish("scheduler.heartbeat", {"time": now})
                except Exception:
                    pass


def start_scheduler(orch, heartbeat_interval: int = 60) -> None:
    """Start the scheduler background thread.

    Safe to call multiple times; starting twice is a no-op.
    """
    global _THREAD, _STOP_EVENT
    hb = max(1, int(heartbeat_interval))
    with _LIFECYCLE_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP_EVENT.clear()
        _get_job_executor()
        _THREAD = threading.Thread(
            target=_scheduler_loop, args=(orch, hb), daemon=True
        )
        _THREAD.start()


def stop_scheduler() -> None:
    """Stop the scheduler background thread.

    Best-effort; may block until the current sleep finishes.
    """
    global _THREAD, _STOP_EVENT, _JOB_EXECUTOR
    with _LIFECYCLE_LOCK:
        _STOP_EVENT.set()
        t = _THREAD
        _THREAD = None
        exe = _JOB_EXECUTOR
        _JOB_EXECUTOR = None
    if t is not None:
        t.join(timeout=5)
    if exe is not None:
        try:
            exe.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


atexit.register(stop_scheduler)
