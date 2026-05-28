"""Cross-platform advisory file locking for tool concurrency.

Extracted from ``todo_tools.py`` so the ``_FileLock`` class lives in its own
module.  ``todo_tools`` re-exports ``_FileLock`` for backward compatibility.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Simple in-process metrics to surface lock fallback behavior. These are
# intentionally lightweight (in-memory) and used for diagnostics / tests.
_lock_metrics_lock = threading.Lock()
_lock_metrics: Dict[str, int] = {
    "stale_reclaims": 0,
    "stale_reclaim_failures": 0,
    "fallback_acquisitions": 0,
    "fallback_acquire_timeouts": 0,
    "fallback_releases": 0,
}


def _inc_lock_metric(key: str) -> None:
    try:
        with _lock_metrics_lock:
            if key in _lock_metrics:
                _lock_metrics[key] += 1
    except Exception as e:
        try:
            logger.debug(
                "Failed to increment lock metric %s: %s\n%s",
                key,
                e,
                traceback.format_exc(),
            )
        except Exception:
            logger.debug("Failed to increment lock metric %s", key)


def get_lock_metrics() -> Dict[str, int]:
    with _lock_metrics_lock:
        return dict(_lock_metrics)


def reset_lock_metrics() -> None:
    with _lock_metrics_lock:
        for k in _lock_metrics:
            _lock_metrics[k] = 0


def _is_network_filesystem(path: Path) -> bool:
    """Best-effort detection whether the given path lives on a network filesystem.

    We try platform-specific strategies: on Linux parse /proc/mounts, on macOS
    parse the output of ``mount``. If detection fails, conservatively return
    False (treat as local filesystem).
    """
    try:
        p = Path(path).resolve()
        path_str = str(p)

        mounts = []
        if sys.platform.startswith("linux"):
            try:
                with open("/proc/mounts", "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 3:
                            mnt = parts[1]
                            fstype = parts[2]
                            mounts.append((mnt, fstype))
            except Exception:
                return False
        elif sys.platform == "darwin":
            try:
                out = subprocess.check_output(
                    ["mount"], stderr=subprocess.DEVNULL, text=True
                )
                for line in out.splitlines():
                    m = re.search(r" on (\S+) \(([^,]+)", line)
                    if m:
                        mounts.append((m.group(1), m.group(2)))
            except Exception:
                return False
        else:
            return False

        best = None
        best_len = -1
        for mnt, fstype in mounts:
            if path_str.startswith(mnt.rstrip("/")) and len(mnt) > best_len:
                best = (mnt, fstype.lower())
                best_len = len(mnt)

        if not best:
            return False

        fstype = best[1]
        network_types = (
            "nfs", "nfs4", "cifs", "smbfs", "smb", "sshfs",
            "fuse.sshfs", "9p", "afs", "coda", "ceph",
        )
        for nt in network_types:
            if nt == fstype or nt in fstype:
                logger.debug(
                    "Filesystem for %s appears to be network type %s", path, fstype
                )
                return True
        return False
    except Exception:
        return False


class FileLock:
    """Cross-platform advisory lock for a given lock path.

    Implementation strategy:
    - On platforms where fcntl is available we use flock() which is released
      automatically if the process exits.
    - Otherwise we fall back to creating an exclusive lockfile using
      os.open(..., O_CREAT|O_EXCL) and removing it on release. This is a
      best-effort fallback for platforms without fcntl.

    The lock blocks until acquired or the timeout is reached.
    """

    def __init__(self, lock_path: Path, timeout: float = 5.0) -> None:
        self.lock_path = Path(lock_path)
        self.timeout = float(timeout)
        self._fp: Any = None
        self._fd: Any = None
        try:
            import fcntl
            self._fcntl = fcntl
        except Exception:
            self._fcntl = None  # type: ignore[assignment]

    def _parse_lockfile(self, data: str):
        m = re.search(r"pid=\s*(\d+)", data)
        hostm = re.search(r"host=([\w\-\.]+)", data)
        tsm = re.search(r"ts=(\d+)", data)
        pid = int(m.group(1)) if m else None
        host = hostm.group(1) if hostm else None
        ts = int(tsm.group(1)) if tsm else None

        if socket is not None:
            same_host = (host == socket.gethostname())
        else:
            same_host = False

        stale_ttl = int(os.environ.get("TODO_LOCK_STALE_TTL", "300"))
        too_old = (
            ts is not None
            and (int(time.time() * 1000) - ts) / 1000.0 > stale_ttl
        )
        return pid, host, ts, same_host, too_old

    def _try_reclaim(self, pid, same_host, too_old) -> bool:
        allow_nfs_env = os.environ.get("TODO_ALLOW_STALE_RECLAIM_ON_NFS", "").lower()
        allow_nfs = allow_nfs_env in ("1", "true", "yes")
        try:
            is_nfs = _is_network_filesystem(self.lock_path)
        except Exception:
            is_nfs = False
        if is_nfs and not allow_nfs:
            logger.warning(
                "Refusing to reclaim stale lockfile %s on network filesystem (pid %s). "
                "Set TODO_ALLOW_STALE_RECLAIM_ON_NFS=1 to override",
                self.lock_path, pid,
            )
            return False
        try:
            os.unlink(str(self.lock_path))
            _inc_lock_metric("stale_reclaims")
            logger.warning(
                "Removed stale lockfile %s (pid %s not running)",
                self.lock_path, pid,
            )
            return True
        except Exception:
            _inc_lock_metric("stale_reclaim_failures")
            logger.exception("Failed to remove stale lockfile %s", self.lock_path)
            return False

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()

        if self._fcntl is not None:
            self._fp = open(self.lock_path, "a+")
            while True:
                try:
                    if self._fp is None:
                        raise TimeoutError("Invalid file handle for flock")
                    self._fcntl.flock(self._fp.fileno(), self._fcntl.LOCK_EX)
                    break
                except InterruptedError:
                    if time.time() - start >= self.timeout:
                        raise TimeoutError("Timeout acquiring lock")
                    continue
            return self

        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        while True:
            try:
                self._fd = os.open(str(self.lock_path), flags)
                try:
                    hostname = socket.gethostname()
                    info = f"pid={os.getpid()} ts={int(time.time() * 1000)} host={hostname}\n"
                    try:
                        stack = traceback.format_stack(limit=5)
                        info += "".join(stack)
                    except Exception:
                        pass
                    os.write(self._fd, info.encode("utf-8"))
                except Exception:
                    pass
                _inc_lock_metric("fallback_acquisitions")
                logger.debug(
                    "Acquired fallback lockfile %s (pid=%s)",
                    self.lock_path, os.getpid(),
                )
                return self
            except FileExistsError:
                try:
                    if self.lock_path.exists():
                        try:
                            data = self.lock_path.read_text(encoding="utf-8")
                            pid, host, ts, same_host, too_old = self._parse_lockfile(data)
                            if pid is not None:
                                try:
                                    os.kill(pid, 0)
                                    if same_host or too_old:
                                        if self._try_reclaim(pid, same_host, too_old):
                                            continue
                                except OSError as e:
                                    if getattr(e, "errno", None) == errno.ESRCH:
                                        if same_host or too_old:
                                            if self._try_reclaim(pid, same_host, too_old):
                                                continue
                                    elif getattr(e, "errno", None) == errno.EPERM:
                                        pass
                                    else:
                                        pass
                            else:
                                if ts is not None and too_old:
                                    if self._try_reclaim(None, False, too_old):
                                        continue
                        except PermissionError:
                            pass
                        except Exception:
                            pass
                except Exception:
                    pass
                if time.time() - start >= self.timeout:
                    _inc_lock_metric("fallback_acquire_timeouts")
                    raise TimeoutError(f"Timeout acquiring lock {self.lock_path}")
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> Optional[bool]:  # type: ignore[exit-return]
        if self._fcntl is not None:
            try:
                if self._fp is not None:
                    try:
                        self._fcntl.flock(self._fp.fileno(), self._fcntl.LOCK_UN)
                    except Exception:
                        pass
            finally:
                try:
                    if self._fp is not None:
                        self._fp.close()
                except Exception:
                    pass
            return False

        try:
            if self._fd is not None:
                os.close(self._fd)
                _inc_lock_metric("fallback_releases")
                logger.debug(
                    "Closed fallback lock fd for %s (pid=%s)",
                    self.lock_path, os.getpid(),
                )
        except Exception:
            pass
        try:
            os.unlink(str(self.lock_path))
            logger.debug("Unlinked fallback lockfile %s", self.lock_path)
        except FileNotFoundError:
            pass
        return False
