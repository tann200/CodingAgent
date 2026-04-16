import os
import sys
import threading
import queue as _queue
import json

# Ensure the repository root is on sys.path so `import src.*` works during pytest
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def pytest_configure(config):
    # Placeholder to ensure conftest is imported early
    return


def recv_json_ws_factory():
    """Return a recv helper for blocking TestClient WebSocket objects.

    Usage in tests: add `recv_json_ws` as a function argument and call
    `recv_json_ws(ws, timeout=2.0)` to receive a JSON message with a timeout.
    """

    def _recv_json_ws(ws, timeout=2.0):
        q = _queue.Queue()

        def _worker():
            try:
                res = ws.receive_json()
                q.put((True, res))
            except Exception as e:
                q.put((False, e))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        try:
            ok, val = q.get(timeout=timeout)
            if ok:
                return val
            raise val
        except _queue.Empty:
            raise TimeoutError("WebSocket receive timed out")

    return _recv_json_ws


import pytest


@pytest.fixture
def recv_json_ws():
    return recv_json_ws_factory()
