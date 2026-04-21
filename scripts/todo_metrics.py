#!/usr/bin/env python3
"""CLI and small HTTP debug endpoint for TODO/lock/RBW metrics.

Usage:
  scripts/todo_metrics.py print        # print current metrics as JSON
  scripts/todo_metrics.py reset        # reset metrics
  scripts/todo_metrics.py serve [port] # serve metrics over HTTP (default port 8000)

The script imports metrics helpers from src.tools.todo_tools. When run as a
subprocess in CI/dev, ensure the repository root is on PYTHONPATH or run this
script from the repo root (it will insert the repo root into sys.path).
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def ensure_repo_on_path():
    # Ensure the repo root is on sys.path so the src package resolves
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))


def print_metrics():
    ensure_repo_on_path()
    from src.tools.todo_tools import get_lock_metrics, get_rbw_metrics

    out = {
        "lock_metrics": get_lock_metrics(),
        "rbw_metrics": get_rbw_metrics(),
    }
    print(json.dumps(out, indent=2))


def reset_metrics():
    ensure_repo_on_path()
    from src.tools.todo_tools import reset_lock_metrics, reset_rbw_metrics

    reset_lock_metrics()
    reset_rbw_metrics()
    print("metrics reset")


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/metrics"):
            self.send_response(404)
            self.end_headers()
            return
        ensure_repo_on_path()
        # If Prometheus integration is enabled and prometheus_client is
        # available, serve the metrics in Prometheus text format. Otherwise
        # fall back to a JSON dump of the in-process counters.
        try:
            from src.tools.todo_metrics import enabled as prometheus_enabled

            if prometheus_enabled():
                try:
                    from prometheus_client import generate_latest  # type: ignore

                    body = generate_latest()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                except Exception:
                    # Fall through to JSON fallback
                    pass
        except Exception:
            # Prometheus wrapper not available — fall back
            pass

        from src.tools.todo_tools import get_lock_metrics, get_rbw_metrics

        out = {"lock_metrics": get_lock_metrics(), "rbw_metrics": get_rbw_metrics()}
        body = json.dumps(out).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/reset":
            ensure_repo_on_path()
            from src.tools.todo_tools import reset_lock_metrics, reset_rbw_metrics

            reset_lock_metrics()
            reset_rbw_metrics()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(404)
        self.end_headers()


def serve(port: int = 8000):
    server = HTTPServer(("", port), MetricsHandler)
    print(f"Serving metrics on http://0.0.0.0:{port}/metrics")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd == "print":
        print_metrics()
        return 0
    if cmd == "reset":
        reset_metrics()
        return 0
    if cmd == "serve":
        port = int(argv[2]) if len(argv) > 2 else 8000
        serve(port)
        return 0
    print("Unknown command", cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
