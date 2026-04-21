"""Lightweight Prometheus metrics wrapper for todo_tools.

This module implements a minimal, optional integration with prometheus_client.
It is intentionally conservative: metrics are disabled by default and only
activated when TODO_PROMETHEUS_ENABLED is truthy (1/true/yes).

The wrapper creates per-metric Counters on demand and exposes a simple
inc_metric(name, amount=1) API that is safe to call from application code.
Failures or missing prometheus_client are non-fatal.
"""

from __future__ import annotations

import os
import threading
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Read enable flag from env at import time
_ENABLED = os.environ.get("TODO_PROMETHEUS_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
)

# Map internal metric keys (used by todo_tools) to Prometheus metric names
_METRIC_NAME_MAP: Dict[str, str] = {
    # Lock metrics
    "stale_reclaims": "codagent_todo_lock_stale_reclaims_total",
    "stale_reclaim_failures": "codagent_todo_lock_stale_reclaim_failures_total",
    "fallback_acquisitions": "codagent_todo_lock_fallback_acquisitions_total",
    "fallback_acquire_timeouts": "codagent_todo_lock_fallback_acquire_timeouts_total",
    "fallback_releases": "codagent_todo_lock_fallback_releases_total",
    # RBW metrics
    "rbw_notify_attempts": "codagent_todo_rbw_notify_attempts_total",
    "rbw_missing_orch": "codagent_todo_rbw_missing_orch_total",
    "rbw_notify_failures": "codagent_todo_rbw_notify_failures_total",
    "rbw_invalidate_failures": "codagent_todo_rbw_invalidate_failures_total",
}

_lock = threading.Lock()
_initialized = False
_counters: Dict[str, Any] = {}


def _init() -> None:
    """Attempt to initialize Prometheus Counters.

    Non-fatal: failures are logged and metrics remain disabled.
    """
    global _initialized
    if not _ENABLED or _initialized:
        return
    with _lock:
        if _initialized:
            return
        try:
            from prometheus_client import Counter  # type: ignore

            for key, name in _METRIC_NAME_MAP.items():
                try:
                    # Create a Counter for each metric name
                    # For multiprocess mode the user must set PROMETHEUS_MULTIPROC_DIR
                    _counters[key] = Counter(name, f"Metric {name}")
                except Exception:
                    logger.exception("Failed to create prometheus counter %s", name)
            _initialized = True
        except Exception:
            # prometheus_client not available or failed to init
            logger.debug("Prometheus client not available; metrics disabled")
            _initialized = False


def enabled() -> bool:
    """Return whether prometheus integration is enabled (env + init success)."""
    if not _ENABLED:
        return False
    if not _initialized:
        _init()
    return _initialized


def inc_metric(key: str, amount: int = 1) -> None:
    """Increment the named metric (best-effort).

    The key must be one of the keys in _METRIC_NAME_MAP. If Prometheus is
    disabled or the counter could not be created, this is a no-op.
    """
    try:
        if not _ENABLED:
            return
        if not _initialized:
            _init()
        if not _initialized:
            return
        c = _counters.get(key)
        if c is None:
            return
        try:
            c.inc(amount)
        except Exception:
            logger.debug("Prometheus counter.inc failed for %s", key, exc_info=True)
    except Exception:
        logger.debug("inc_metric failed for %s", key, exc_info=True)
