import threading
import time

from src.tools.todo_tools import (
    _FileLock,
    _lock_path,
    get_lock_metrics,
    reset_lock_metrics,
)


def test_lock_metrics_reset_and_get():
    reset_lock_metrics()
    metrics = get_lock_metrics()
    # All metrics should start at zero after a reset
    assert all(v == 0 for v in metrics.values())


def test_metrics_fallback_acquire_and_release(tmp_path):
    reset_lock_metrics()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))

    lock = _FileLock(lockp, timeout=2.0)
    # Force fallback path
    lock._fcntl = None

    with lock:
        metrics = get_lock_metrics()
        assert metrics["fallback_acquisitions"] >= 1

    # After release, fallback_releases should have incremented
    metrics = get_lock_metrics()
    assert metrics["fallback_releases"] >= 1


def test_metrics_fallback_acquire_timeout(tmp_path):
    reset_lock_metrics()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))

    # Acquire lock in main thread and hold it
    lock1 = _FileLock(lockp, timeout=2.0)
    lock1._fcntl = None

    def worker_attempt():
        # This attempt should time out while lock1 holds the lock
        try:
            l2 = _FileLock(lockp, timeout=0.2)
            l2._fcntl = None
            with l2:
                pass
        except TimeoutError:
            # Expected in this test
            return

    with lock1:
        # Start a worker that will try to acquire and should time out
        t = threading.Thread(target=worker_attempt)
        t.start()
        t.join()
        # Ensure the acquisition by lock1 succeeded
        metrics = get_lock_metrics()
        assert metrics["fallback_acquisitions"] >= 1

    # After release and worker timeout, timeout metric should be incremented
    metrics = get_lock_metrics()
    assert metrics["fallback_acquire_timeouts"] >= 1


def test_metrics_stale_reclaim(tmp_path):
    reset_lock_metrics()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))
    lockp.parent.mkdir(parents=True, exist_ok=True)

    # Create a fake stale lock with a high PID unlikely to exist
    fake_pid = 999999
    ts = int(time.time() * 1000)
    import socket, os

    # Make reclaim immediate for the test
    os.environ["TODO_LOCK_STALE_TTL"] = "0"
    hostname = socket.gethostname()
    lockp.write_text(f"pid={fake_pid} ts={ts} host={hostname}\n")

    lock = _FileLock(lockp, timeout=2.0)
    lock._fcntl = None

    with lock:
        metrics = get_lock_metrics()
        # We should have reclaimed the stale lock and acquired the fallback lock
        assert metrics["stale_reclaims"] >= 1
        assert metrics["fallback_acquisitions"] >= 1

    metrics = get_lock_metrics()
    assert metrics["fallback_releases"] >= 1
