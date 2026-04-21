TODO Metrics (Prometheus)
=========================

Overview
--------
This project exposes lightweight in-process metrics for TODO locking and RBW
operations. Prometheus integration is optional and disabled by default.

Enabling Prometheus
-------------------
- Set the environment variable `TODO_PROMETHEUS_ENABLED=1` (or `true`/`yes`) to
  enable Prometheus counters. The code will attempt to import
  `prometheus_client` and create per-metric Counters. If the package is not
  available, metrics stay disabled and the application continues to work.

Metrics exposed
---------------
The wrapper maps internal metric keys to Prometheus Counters. Important
examples:

- `codagent_todo_lock_fallback_acquisitions_total` — number of fallback lock
  acquisitions
- `codagent_todo_lock_fallback_acquire_timeouts_total` — number of timeouts
  while acquiring the fallback lock
- `codagent_todo_lock_stale_reclaims_total` — number of stale lockfile
  reclaims attempted
- `codagent_todo_rbw_notify_failures_total` — RBW notification failures

Multiprocess considerations
--------------------------
If the agent runs multiple processes (workers) you must choose a strategy to
aggregate metrics:

1. Prometheus client multiprocess mode
   - Set `PROMETHEUS_MULTIPROC_DIR` to a writable directory before importing
     `prometheus_client`.
   - Use the `prometheus_client` multiprocess collector when exposing the
     metrics endpoint. See the `prometheus_client` docs for details.

2. Aggregator/sidecar
   - Run a single process that exposes metrics and aggregates events from
     workers (e.g., via IPC or a socket). Workers update local state and the
     aggregator exposes the combined view.

3. Pushgateway (not recommended)
   - For short-lived jobs you can push metrics to Pushgateway, but this is
     usually less suitable for long-running agent processes.

Notes & guidance
----------------
- Keep label cardinality low — do not add per-workdir or per-task labels.
- Metric updates are best-effort and non-blocking; failures to update metrics
  will never affect core logic.
- For testing, the code provides a small wrapper at
  `src/tools/todo_metrics.py` that can be mocked in unit tests.

Using the debug HTTP server
---------------------------
The repository includes `scripts/todo_metrics.py` which provides a small HTTP
endpoint useful for debugging. By default it serves a JSON dump of the
in-process counters at `/metrics`. If Prometheus integration is enabled
(`TODO_PROMETHEUS_ENABLED=1`) and `prometheus_client` is available, the
endpoint will serve the standard Prometheus text exposition format instead.

Examples:

  # Print metrics as JSON
  python scripts/todo_metrics.py print

  # Serve on port 8000
  python scripts/todo_metrics.py serve 8000
