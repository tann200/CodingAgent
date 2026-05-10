import os
import socket
import time

from src.tools.file_lock import FileLock, get_lock_metrics, reset_lock_metrics
from src.tools.todo_tools import _lock_path


def test_do_not_reclaim_on_network_fs(tmp_path, monkeypatch):
    reset_lock_metrics()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))
    lockp.parent.mkdir(parents=True, exist_ok=True)

    fake_pid = 999999
    ts = int(time.time() * 1000)
    os.environ["TODO_LOCK_STALE_TTL"] = "0"

    hostname = socket.gethostname()
    lockp.write_text(f"pid={fake_pid} ts={ts} host={hostname}\n")

    # Force detection to report network FS
    monkeypatch.setattr("src.tools.file_lock._is_network_filesystem", lambda p: True)

    lock = FileLock(lockp, timeout=1.0)
    lock._fcntl = None

    try:
        with lock:
            pass
    except TimeoutError:
        pass

    # Lockfile must NOT have been reclaimed on network FS
    assert lockp.exists()
    metrics = get_lock_metrics()
    assert metrics["stale_reclaims"] == 0
