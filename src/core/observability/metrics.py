"""Lightweight in-process metrics store for single-developer diagnostics.

This module provides a simple, thread-safe MetricsStore with rolling
histogram support. It's intentionally minimal and dependency-free so it can
be used in local development and tests without introducing external
dependencies.

Usage:
    from src.core.observability.metrics import metrics
    metrics.increment_counter("some_counter")
    metrics.record_histogram("tool.exec_ms", 12.3)
    snapshot = metrics.snapshot()
"""

from collections import deque
import threading
from statistics import mean
import time
import asyncio
from typing import Dict, Optional, Callable


class RollingHistogram:
    def __init__(self, maxlen: int = 1000) -> None:
        self._samples = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, value: float) -> None:
        with self._lock:
            self._samples.append(float(value))

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            s = list(self._samples)
        if not s:
            return {"count": 0}
        s_sorted = sorted(s)
        n = len(s_sorted)

        def pct(p: float) -> float:
            idx = min(int(round((p / 100.0) * (n - 1))), n - 1)
            return float(s_sorted[idx])

        return {
            "count": n,
            "min": float(s_sorted[0]),
            "max": float(s_sorted[-1]),
            "mean": float(mean(s_sorted)),
            "p50": pct(50),
            "p95": pct(95),
            "p99": pct(99),
        }

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()


class MetricsStore:
    def __init__(self) -> None:
        self._counters: Dict[str, int] = {}
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, RollingHistogram] = {}
        self._lock = threading.Lock()

    # Counters
    def increment_counter(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + int(amount)

    # Gauges
    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    # Histograms
    def record_histogram(self, name: str, value: float, maxlen: int = 1000) -> None:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = RollingHistogram(maxlen=maxlen)
            hist = self._histograms[name]
        hist.record(value)

    def reset(self) -> None:
        """Clear all stored counters, gauges, and histograms."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            # clear histograms in-place to avoid replacing objects that other
            # threads may hold references to
            for h in self._histograms.values():
                try:
                    h.clear()
                except Exception:
                    pass

    def snapshot(self) -> Dict[str, object]:
        # Return a lightweight snapshot of counters/gauges/histograms
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {k: v.snapshot() for k, v in self._histograms.items()}
        return {"counters": counters, "gauges": gauges, "histograms": histograms}


# Module-level singleton
metrics = MetricsStore()


def get_metrics_snapshot() -> Dict[str, object]:
    return metrics.snapshot()


class Timer:
    """Context manager to time a block and record into the metrics store.

    Usage:
        with Timer("tool.exec_ms"):
            do_work()
    """

    def __init__(self, name: str, store: Optional[MetricsStore] = None) -> None:
        self.name = name
        self._store = store or metrics
        self._t0: Optional[float] = None

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._t0 is not None:
                elapsed_ms = (time.monotonic() - self._t0) * 1000.0
                self._store.record_histogram(self.name, elapsed_ms)
        except Exception:
            # Best-effort: metrics must not raise
            pass


def timed(name: str) -> Callable:
    """Decorator to time function execution and record into a histogram.

    Works with both sync and async functions.
    """

    def decorator(fn: Callable):
        if asyncio.iscoroutinefunction(fn):

            async def _awrapper(*args, **kwargs):
                t0 = time.monotonic()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    try:
                        elapsed_ms = (time.monotonic() - t0) * 1000.0
                        metrics.record_histogram(name, elapsed_ms)
                    except Exception:
                        pass

            return _awrapper

        def _wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                try:
                    elapsed_ms = (time.monotonic() - t0) * 1000.0
                    metrics.record_histogram(name, elapsed_ms)
                except Exception:
                    pass

        return _wrapper

    return decorator
