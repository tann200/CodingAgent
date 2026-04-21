TODO Metrics (in-process)
=========================

Overview
--------
This project exposes lightweight in-process metrics for TODO locking and RBW
operations. Metrics are kept in-memory and are intended for local diagnostics
and unit tests; there is no built-in Prometheus export in this repository.

Metrics available
-----------------
The code exposes two small metric groups (plain integers):

- Lock metrics: `stale_reclaims`, `stale_reclaim_failures`,
  `fallback_acquisitions`, `fallback_acquire_timeouts`, `fallback_releases`.
- RBW metrics: `rbw_notify_attempts`, `rbw_missing_orch`,
  `rbw_notify_failures`, `rbw_invalidate_failures`.

Accessing metrics
-----------------
For simple debugging you can print the in-process metrics as JSON. Example:

  python - <<PY
  from src.tools.todo_tools import get_lock_metrics, get_rbw_metrics
  import json
  print(json.dumps({
      'lock_metrics': get_lock_metrics(),
      'rbw_metrics': get_rbw_metrics()
  }, indent=2))
  PY

This project intentionally keeps metrics local to avoid adding run-time
dependencies and complexity for solo-developer workflows. For common
single-developer use-cases, prefer these minimal options which require no
third-party dependencies.

CLI dump (no dependencies)
---------------------------
Create a small script to print metrics as JSON for quick inspection. Place
it under `scripts/dump_metrics.py` and run when needed.

```python
# scripts/dump_metrics.py
import json
from src.tools.todo_tools import get_lock_metrics, get_rbw_metrics

if __name__ == "__main__":
    out = {
        "lock_metrics": get_lock_metrics(),
        "rbw_metrics": get_rbw_metrics(),
    }
    print(json.dumps(out, indent=2))
```

Minimal JSON HTTP adapter (standard library only)
-------------------------------------------------
Run a tiny HTTP server when you need an HTTP endpoint. This requires no
third-party packages and can be started only when you want it.

```python
# tools/json_metrics_server.py
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from src.tools.todo_tools import get_lock_metrics, get_rbw_metrics

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        payload = {
            "lock_metrics": get_lock_metrics(),
            "rbw_metrics": get_rbw_metrics(),
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8001), Handler).serve_forever()
```

These scripts keep the repository dependency-free while giving you simple
HTTP or CLI access to the in-process counters. If you later need scraping or
long-term retention, add a separately-run adapter or a monitoring stack — do
not bake that into the main repo.

Advanced (optional)
-------------------
If you want richer local histograms and programmatic snapshots, this repo
includes a lightweight metrics store at `src/core/observability/metrics.py`.
Use the module-level `metrics` singleton for counters, gauges, and rolling
histograms:

```py
from src.core.observability.metrics import metrics
metrics.increment_counter("my.counter")
metrics.record_histogram("tool.exec_ms", 12.3)
print(metrics.snapshot())
```

The store is intentionally small and thread-safe; it is suitable for
single-developer debugging and unit tests.
