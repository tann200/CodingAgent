import os
import sys
import threading
import queue as _queue

# ruff: noqa: E501

# Ensure the repository root is on sys.path so `import src.*` works during pytest
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def pytest_configure(config):
    # Register custom markers to avoid PyTestUnknownMarkWarning and ensure
    # test authors can mark tests that need threads synchronised.
    try:
        config.addinivalue_line(
            "markers",
            "sync_threads: run threading.Thread targets synchronously for deterministic tests",
        )
    except Exception:
        # Best-effort; not fatal in constrained test environments
        pass
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


# Minimal synchronous Thread and ThreadPoolExecutor replacements used by
# the sync_threads fixture. These run submitted callables inline so tests
# that opt in via the fixture/marker become deterministic even when code
# under test uses ThreadPoolExecutor.
class _SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args or ()
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        return None


class _SyncFuture:
    def __init__(self, fn, *args, **kwargs):
        try:
            self._result = fn(*args, **kwargs)
            self._exception = None
        except Exception as e:
            self._result = None
            self._exception = e

    def result(self, timeout=None):
        if self._exception:
            raise self._exception
        return self._result

    def add_done_callback(self, cb):
        try:
            cb(self)
        except Exception:
            # Callbacks must not raise inside tests
            pass

    def cancel(self):
        return False

    def cancelled(self):
        return False

    def done(self):
        return True


class _SyncThreadPoolExecutor:
    def __init__(self, max_workers=None, thread_name_prefix=""):
        # Parameters ignored; synchronous executor runs tasks inline
        pass

    def submit(self, fn, *args, **kwargs):
        return _SyncFuture(fn, *args, **kwargs)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        for args in zip(*iterables):
            yield fn(*args)

    def shutdown(self, wait=True, cancel_futures=False):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()


@pytest.fixture
def recv_json_ws():
    return recv_json_ws_factory()


@pytest.fixture
def sync_threads(monkeypatch):
    """Monkeypatch threading.Thread so .start() runs the target synchronously.

    Use in tests that need background workers to execute inline for determinism:

        def test_x(sync_threads):
            ...

    The patched Thread supports (target, args=(), kwargs=None, daemon=None).
    """
    import threading as _threading
    import concurrent.futures as _futures

    monkeypatch.setattr(_threading, "Thread", _SyncThread)
    # Replace ThreadPoolExecutor so executor.submit(...) executes inline
    monkeypatch.setattr(_futures, "ThreadPoolExecutor", _SyncThreadPoolExecutor)
    yield


@pytest.fixture(autouse=True)
def _apply_sync_threads_marker(request, monkeypatch):
    """Autouse helper: if a test is marked with @pytest.mark.sync_threads
    apply the same threading.Thread patch as the sync_threads fixture.

    This lets tests opt-in via marker instead of requesting the fixture.
    """
    marker = request.node.get_closest_marker("sync_threads")
    if not marker:
        # Nothing to do for unmarked tests
        yield
        return

    import threading as _threading
    import concurrent.futures as _futures

    monkeypatch.setattr(_threading, "Thread", _SyncThread)
    monkeypatch.setattr(_futures, "ThreadPoolExecutor", _SyncThreadPoolExecutor)
    yield
