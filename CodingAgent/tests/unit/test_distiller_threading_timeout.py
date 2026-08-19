import time
import threading
import concurrent.futures

from src.core.memory.distiller import _get_distiller_executor


def test_distiller_executor_timeout_does_not_kill_thread() -> None:
    """Ensure future.result(timeout=...) only stops the waiting thread.

    The worker thread should continue to run to completion even if the
    waiting thread times out. Also verify the executor is a singleton.
    """
    ex = _get_distiller_executor()
    completed = threading.Event()

    def _worker() -> str:
        # short sleep to simulate work
        time.sleep(0.05)
        completed.set()
        return "done"

    fut = ex.submit(_worker)

    try:
        # Very small timeout to force a TimeoutError from the waiter
        fut.result(timeout=0.001)
        assert False, "Expected TimeoutError from fut.result(timeout=...)"
    except concurrent.futures.TimeoutError:
        # expected: the waiter timed out but the worker should continue
        pass

    # Worker should still complete eventually
    assert completed.wait(timeout=1.0), "Worker thread did not finish after timeout"

    # Confirm executor is reused (singleton)
    assert ex is _get_distiller_executor()
