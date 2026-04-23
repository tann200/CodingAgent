import os
import time

from src.tools.todo_tools import _lock_path, _FileLock, _is_network_filesystem


def test_do_not_reclaim_on_network_fs(tmp_path, monkeypatch):
    # Simulate network FS and ensure stale reclaim is conservative
    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))
    lockp.parent.mkdir(parents=True, exist_ok=True)

    fake_pid = 999999
    ts = int(time.time() * 1000)
    os.environ["TODO_LOCK_STALE_TTL"] = "0"

    # Force detection to report network FS
    monkeypatch.setattr("src.tools.todo_tools._is_network_filesystem", lambda p: True)

    hostname = __import__("socket").gethostname()
    lockp.write_text(f"pid={fake_pid} ts={ts} host={hostname}\n")

    lock = _FileLock(lockp, timeout=1.0)
    lock._fcntl = None

    try:
        with lock:
            # If reclaim happened on network FS, acquisition would succeed — we expect it to block until timeout
            pass
    except TimeoutError:
        # Expected: reclaim should not occur on network FS and acquiring should time out
        return
    # If we didn't timeout, ensure we did not remove the original lock
    assert lockp.exists()


def test_allow_reclaim_on_network_fs_with_flag(tmp_path, monkeypatch):
    # If env var explicitly allows reclaim on NFS, the reclaim should proceed
    workdir = tmp_path / "repo"
    workdir.mkdir()
    lockp = _lock_path(str(workdir))
    lockp.parent.mkdir(parents=True, exist_ok=True)

    fake_pid = 999999
    ts = int(time.time() * 1000)
    os.environ["TODO_LOCK_STALE_TTL"] = "0"
    os.environ["TODO_ALLOW_STALE_RECLAIM_ON_NFS"] = "1"

    monkeypatch.setattr("src.tools.todo_tools._is_network_filesystem", lambda p: True)

    hostname = __import__("socket").gethostname()
    lockp.write_text(f"pid={fake_pid} ts={ts} host={hostname}\n")

    lock = _FileLock(lockp, timeout=2.0)
    lock._fcntl = None

    with lock:
        # Should have reclaimed and acquired
        assert lockp.exists()
