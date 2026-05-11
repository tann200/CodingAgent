"""Scheduler bootstrap helper for the orchestrator."""

from __future__ import annotations

import time
from typing import Any

from src.core.logger import logger as guilogger


def _init_scheduler(orch: Any) -> None:
    """Initialize and start the lightweight scheduler for the orchestrator.

    Kept as a small, standalone helper so bootstrap_orchestrator() remains
    a thin wrapper. This function is conservative and fails silently on any
    error to avoid impacting startup.
    """
    try:
        import os as _os

        from src.core.scheduler import worker as _sched

        _hb = int(_os.getenv("CODING_AGENT_SCHEDULER_HEARTBEAT", "60") or 60)
        _dist_int = int(_os.getenv("CODING_AGENT_DISTILL_INTERVAL", "600") or 600)

        def _publish_distill_request() -> None:
            try:
                orch.event_bus.publish(
                    "scheduler.distill_request",
                    {"source": "scheduler", "time": time.time()},
                )
            except Exception:
                try:
                    guilogger.warning("Scheduler: failed to publish distill_request")
                except Exception:
                    pass

        try:
            from src.core.config_loader import get as _cfg_get

            _sched_cfg = _cfg_get("scheduler_jobs", {}) or {}
        except Exception:
            _sched_cfg = {}

        _pd_cfg = (
            _sched_cfg.get("periodic_distill_request", {})
            if isinstance(_sched_cfg, dict)
            else {}
        )
        _pd_enabled = _pd_cfg.get("enabled", True)
        _pd_interval = int(_pd_cfg.get("interval", _dist_int))

        if _pd_enabled:
            _sched.register_job(
                "periodic_distill_request", _publish_distill_request, _pd_interval
            )
        _sched.start_scheduler(orch, heartbeat_interval=_hb)
        orch._scheduler = _sched
        try:
            orch.lifecycle_manager.on_shutdown(
                "stop_scheduler", lambda _sid: _sched.stop_scheduler()
            )
        except Exception:
            pass
    except Exception:
        # Never fail orchestrator bootstrap because scheduler init failed.
        pass
