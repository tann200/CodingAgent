"""Regression: fallback locking must never evict a live owner.

Audit §4 High: previously, on the fallback (non-fcntl) path a live same-host
PID was reclaimed after a successful ``os.kill(pid, 0)`` — i.e. the "is the
process alive?" probe succeeded and the lock was removed anyway.
"""

import os
import socket
import time

import pytest

from src.tools.file_lock import FileLock, get_lock_metrics, reset_lock_metrics
from src.tools.todo_tools import _lock_path


def test_live_holder_never_reclaimed(tmp_path, monkeypatch):
    reset_lock_metrics()
    monkeypatch.setenv("TODO_LOCK_STALE_TTL", "0")

    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))
    lockp.parent.mkdir(parents=True, exist_ok=True)

    # Current process is alive — this lockfile is owned by a LIVE holder.
    live_pid = os.getpid()
    ts = int(time.time() * 1000)
    lockp.write_text(f"pid={live_pid} ts={ts} host={socket.gethostname()}\n")

    lock = FileLock(lockp, timeout=0.3)
    lock._fcntl = None  # force fallback path

    # The holder is alive: acquisition must time out, not reclaim.
    with pytest.raises(TimeoutError):
        lock.acquire()

    # Lockfile must still belong to the live owner; nothing reclaimed.
    assert lockp.exists()
    assert f"pid={live_pid}" in lockp.read_text()
    metrics = get_lock_metrics()
    assert metrics["stale_reclaims"] == 0


def test_dead_owner_still_reclaimed(tmp_path, monkeypatch):
    reset_lock_metrics()
    monkeypatch.setenv("TODO_LOCK_STALE_TTL", "0")

    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))
    lockp.parent.mkdir(parents=True, exist_ok=True)

    # PID 999999 almost certainly does not exist.
    lockp.write_text(
        f"pid=999999 ts={int(time.time() * 1000)} host={socket.gethostname()}\n"
    )

    lock = FileLock(lockp, timeout=2.0)
    lock._fcntl = None

    with lock:
        assert lockp.exists()
    assert not lockp.exists()
    metrics = get_lock_metrics()
    assert metrics["stale_reclaims"] >= 1
