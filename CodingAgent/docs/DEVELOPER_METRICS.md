Developer Metrics Guide
=======================

This project provides lightweight, in-process observability primitives intended
for single-developer workflows. The goals are:

- zero runtime dependencies for core code
- safe, bounded memory usage
- easy programmatic access for tests and debugging

Quick start
-----------

- CLI dump: `python scripts/dump_metrics.py`
- JSON HTTP endpoint: `python tools/json_metrics_server.py` and visit
  `http://127.0.0.1:8001/metrics`

Programmatic API
---------------

Use the `metrics` singleton in `src/core/observability/metrics.py`:

```py
from src.core.observability.metrics import metrics, Timer

metrics.increment_counter("my.counter")
metrics.set_gauge("active_sessions", 3)
with Timer("tool.exec_ms"):
    do_work()
print(metrics.snapshot())
```

Timing helpers
--------------

Use `Timer` as a context manager or the `@timed("name")` decorator to record
function durations into rolling histograms. Histograms are bounded (maxlen=1000
by default) to avoid unbounded memory growth.

Testing guidance
----------------

- Call `metrics.reset()` in test setup/teardown to isolate tests.
- Prefer low-cardinality keys (no dynamic labels) to avoid runaway metrics.
