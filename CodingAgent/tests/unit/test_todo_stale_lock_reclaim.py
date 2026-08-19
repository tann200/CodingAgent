import time

import os
from src.tools.todo_tools import _lock_path, _FileLock
import socket


def test_stale_lock_reclaim(tmp_path):
    """Simulate a stale lockfile and ensure the fallback lock reclaims it.

    The test creates a lockfile with a PID that does not exist and then
    attempts to acquire the lock using the fallback path (force _fcntl=None).
    The lock implementation should detect the stale PID and remove the lockfile
    so acquisition succeeds.
    """
    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))
    lockp.parent.mkdir(parents=True, exist_ok=True)

    # Create a fake stale lock with a high PID unlikely to exist
    fake_pid = 999999
    ts = int(time.time() * 1000)
    # Ensure reclaim TTL is zero for the test so reclaim is attempted immediately
    os.environ["TODO_LOCK_STALE_TTL"] = "0"
    hostname = socket.gethostname()
    lockp.write_text(f"pid={fake_pid} ts={ts} host={hostname}\n")

    # Force the fallback path by instantiating the lock and clearing _fcntl
    lock = _FileLock(lockp, timeout=2.0)
    lock._fcntl = None

    # Should acquire despite the pre-existing stale lockfile
    with lock:
        assert lockp.exists(), "Lockfile should exist while lock is held"

    # After release the lockfile should be removed
    assert not lockp.exists(), "Lockfile should be removed after release"
