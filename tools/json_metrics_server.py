#!/usr/bin/env python3
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


def run(host: str = "127.0.0.1", port: int = 8001) -> None:
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    run()
