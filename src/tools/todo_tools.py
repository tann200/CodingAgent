"""
TODO tracking tool for the coding agent.

Manages a human-readable TODO.md file at .agent-context/TODO.md so the user
can see task progress in real time and the agent can track which steps are done.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import threading

from src.tools._tool import tool
from src.tools.tools_config import agent_context_path

logger = logging.getLogger(__name__)

_TODO_FILENAME = "TODO.md"
_TODO_JSON_FILENAME = "todo.json"

# TTL (seconds) after which a lockfile from another host may be considered stale
# and eligible for reclaim. Can be overridden via environment variable
# TODO_LOCK_STALE_TTL (seconds).
_STALE_LOCK_TTL = int(os.environ.get("TODO_LOCK_STALE_TTL", "300"))

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
        # Best-effort: update Prometheus gauges if enabled
        try:
            if _prometheus_enabled:
                _sync_prometheus_metrics()
        except Exception:
            # Never let metrics syncing interfere with normal flow
            logger.debug("prometheus sync failed", exc_info=True)
    except Exception:
        # Metrics must never interfere with normal operation
        logger.debug("Failed to increment lock metric %s", key, exc_info=True)


def get_lock_metrics() -> Dict[str, int]:
    with _lock_metrics_lock:
        return dict(_lock_metrics)


def reset_lock_metrics() -> None:
    with _lock_metrics_lock:
        for k in _lock_metrics:
            _lock_metrics[k] = 0


# Read-Before-Write (RBW) / notifier metrics
_rbw_metrics_lock = threading.Lock()
_rbw_metrics: Dict[str, int] = {
    "rbw_notify_attempts": 0,
    "rbw_missing_orch": 0,
    "rbw_notify_failures": 0,
    "rbw_invalidate_failures": 0,
}


def _inc_rbw_metric(key: str) -> None:
    try:
        with _rbw_metrics_lock:
            if key in _rbw_metrics:
                _rbw_metrics[key] += 1
        try:
            if _prometheus_enabled:
                _sync_prometheus_metrics()
        except Exception:
            logger.debug("prometheus sync failed", exc_info=True)
    except Exception:
        logger.debug("Failed to increment rbw metric %s", key, exc_info=True)


def get_rbw_metrics() -> Dict[str, int]:
    with _rbw_metrics_lock:
        return dict(_rbw_metrics)


def reset_rbw_metrics() -> None:
    with _rbw_metrics_lock:
        for k in _rbw_metrics:
            _rbw_metrics[k] = 0


# Optional Prometheus export (best-effort). If prometheus_client is available
# we create Gauges and keep them in sync with the in-memory metrics.
_prometheus_enabled = False
_prom_lock = threading.Lock()
_prom_gauges: Dict[str, Any] = {}


def _init_prometheus():
    global _prometheus_enabled, _prom_gauges
    try:
        # prometheus_client is optional; silence static import errors with type ignore
        from prometheus_client import Gauge  # type: ignore

        with _prom_lock:
            # Lock metrics
            _prom_gauges.update(
                {k: Gauge(f"codagent_lock_{k}", k) for k in _lock_metrics}
            )
            # RBW metrics
            _prom_gauges.update(
                {k: Gauge(f"codagent_rbw_{k}", k) for k in _rbw_metrics}
            )
        _prometheus_enabled = True
    except Exception:
        _prometheus_enabled = False


def _sync_prometheus_metrics() -> None:
    if not _prometheus_enabled:
        return
    with _prom_lock:
        # Sync lock metrics
        for k, v in get_lock_metrics().items():
            gauge = _prom_gauges.get(k)
            try:
                if gauge is not None:
                    gauge.set(v)
            except Exception:
                logger.debug("Failed to set prometheus gauge for %s", k, exc_info=True)
        # Sync RBW metrics
        for k, v in get_rbw_metrics().items():
            gauge = _prom_gauges.get(k)
            try:
                if gauge is not None:
                    gauge.set(v)
            except Exception:
                logger.debug("Failed to set prometheus gauge for %s", k, exc_info=True)


# Try to initialize prometheus gauges lazily
try:
    _init_prometheus()
except Exception:
    _prometheus_enabled = False


def _todo_path(workdir: str) -> Path:
    return agent_context_path(Path(workdir)) / _TODO_FILENAME


def _todo_json_path(workdir: str) -> Path:
    return agent_context_path(Path(workdir)) / _TODO_JSON_FILENAME


def _lock_path(workdir: str) -> Path:
    """Return the path to the per-workdir lock file used to serialize TODO writes."""
    return agent_context_path(Path(workdir)) / ".todo.lock"


def _is_network_filesystem(path: Path) -> bool:
    """Best-effort detection whether the given path lives on a network filesystem.

    We try platform-specific strategies: on Linux parse /proc/mounts, on macOS
    parse the output of `mount`. If detection fails, conservatively return
    False (treat as local filesystem).
    """
    try:
        import sys
        import subprocess
        import re

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
            # Unknown platform: don't assume network FS
            return False

        # Find the most specific mountpoint that is a prefix of the path
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
            "nfs",
            "nfs4",
            "cifs",
            "smbfs",
            "smb",
            "sshfs",
            "fuse.sshfs",
            "9p",
            "afs",
            "coda",
            "ceph",
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


class _FileLock:
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
        # _fp/_fd are used only for platform-specific locking; use Any to
        # avoid static type complaints in editors.
        self._fp: Any = None
        self._fd: Any = None
        try:
            import fcntl

            self._fcntl = fcntl
        except Exception:
            self._fcntl = None

    def __enter__(self):
        # Ensure parent dir exists
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        if self._fcntl is not None:
            # Use flock on an open file descriptor
            self._fp = open(self.lock_path, "a+")
            while True:
                try:
                    # Use fileno() to satisfy static type checkers which expect
                    # an integer file descriptor for fcntl.flock.
                    if self._fp is None:
                        raise TimeoutError("Invalid file handle for flock")
                    self._fcntl.flock(self._fp.fileno(), self._fcntl.LOCK_EX)
                    break
                except InterruptedError:
                    if time.time() - start >= self.timeout:
                        raise TimeoutError("Timeout acquiring lock")
                    continue
            return self

        # Fallback: create an exclusive lockfile using O_EXCL
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        while True:
            try:
                # Will raise FileExistsError if another holder exists
                self._fd = os.open(str(self.lock_path), flags)
                # Write diagnostic info into the lockfile: PID and timestamp
                try:
                    import socket

                    hostname = socket.gethostname()
                    info = f"pid={os.getpid()} ts={int(time.time() * 1000)} host={hostname}\n"
                    # Include a small stack fragment for debugging; don't import traceback at module import time
                    try:
                        import traceback

                        stack = traceback.format_stack(limit=5)
                        info += "".join(stack)
                    except Exception:
                        pass
                    os.write(self._fd, info.encode("utf-8"))
                except Exception:
                    # Diagnostics are best-effort
                    pass
                _inc_lock_metric("fallback_acquisitions")
                logger.debug(
                    "Acquired fallback lockfile %s (pid=%s)",
                    self.lock_path,
                    os.getpid(),
                )
                return self
            except FileExistsError:
                # Read lockfile contents for diagnostics and attempt to reclaim
                # stale lockfiles when the owning PID is not running.
                try:
                    if self.lock_path.exists():
                        try:
                            data = self.lock_path.read_text(encoding="utf-8")
                            logger.debug(
                                "Lockfile %s exists, contents:\n%s",
                                self.lock_path,
                                data,
                            )
                            # Parse pid if present and check whether the process exists
                            try:
                                import re

                                m = re.search(r"pid=\s*(\d+)", data)
                                hostm = re.search(r"host=([\w\-\.]+)", data)
                                tsm = re.search(r"ts=(\d+)", data)
                                existing_pid = int(m.group(1)) if m else None
                                existing_host = hostm.group(1) if hostm else None
                                existing_ts = int(tsm.group(1)) if tsm else None
                                try:
                                    # If the lock was written by a different host, only
                                    # consider reclaiming it if it is older than TTL.
                                    # Evaluate TTL at runtime so tests can adjust via env var
                                    stale_ttl = int(
                                        os.environ.get("TODO_LOCK_STALE_TTL", "300")
                                    )
                                    # socket may not have been imported earlier in this
                                    # code path (we import it when creating lockfiles),
                                    # so import locally here.
                                    try:
                                        import socket
                                    except Exception:
                                        socket = None

                                    same_host = (
                                        existing_host == socket.gethostname()
                                        if socket is not None
                                        else False
                                    )
                                    too_old = (
                                        existing_ts is not None
                                        and (int(time.time() * 1000) - existing_ts)
                                        / 1000.0
                                        > stale_ttl
                                    )
                                    logger.debug(
                                        "Parsed lockfile: pid=%s host=%s ts=%s same_host=%s too_old=%s",
                                        existing_pid,
                                        existing_host,
                                        existing_ts,
                                        same_host,
                                        too_old,
                                    )

                                    if existing_pid is not None:
                                        try:
                                            os.kill(existing_pid, 0)
                                            # Process exists; do not reclaim
                                            pass
                                        except OSError as e:
                                            # errno.ESRCH -> no such process, errno.EPERM -> no permission
                                            try:
                                                import errno

                                                if (
                                                    getattr(e, "errno", None)
                                                    == errno.ESRCH
                                                ):
                                                    # PID not present — if same host reclaim immediately,
                                                    # otherwise reclaim only if too_old
                                                    if same_host or too_old:
                                                        # Do not attempt to reclaim locks on network
                                                        # filesystems unless explicitly allowed via env var.
                                                        try:
                                                            is_nfs = (
                                                                _is_network_filesystem(
                                                                    self.lock_path
                                                                )
                                                            )
                                                        except Exception:
                                                            is_nfs = False
                                                        allow_nfs = os.environ.get(
                                                            "TODO_ALLOW_STALE_RECLAIM_ON_NFS",
                                                            "",
                                                        ).lower() in (
                                                            "1",
                                                            "true",
                                                            "yes",
                                                        )
                                                        if is_nfs and not allow_nfs:
                                                            logger.warning(
                                                                "Refusing to reclaim stale lockfile %s on network filesystem (pid %s). Set TODO_ALLOW_STALE_RECLAIM_ON_NFS=1 to override",
                                                                self.lock_path,
                                                                existing_pid,
                                                            )
                                                            # Treat as active; allow caller to retry until timeout
                                                            pass
                                                        else:
                                                            try:
                                                                os.unlink(
                                                                    str(self.lock_path)
                                                                )
                                                                _inc_lock_metric(
                                                                    "stale_reclaims"
                                                                )
                                                                logger.warning(
                                                                    "Removed stale lockfile %s (pid %s not running)",
                                                                    self.lock_path,
                                                                    existing_pid,
                                                                )
                                                                continue
                                                            except Exception:
                                                                _inc_lock_metric(
                                                                    "stale_reclaim_failures"
                                                                )
                                                                logger.exception(
                                                                    "Failed to remove stale lockfile %s",
                                                                    self.lock_path,
                                                                )
                                                elif (
                                                    getattr(e, "errno", None)
                                                    == errno.EPERM
                                                ):
                                                    # Process exists but we cannot signal it; treat as active
                                                    pass
                                                else:
                                                    # Unknown OSError: be conservative and treat as active
                                                    pass
                                            except Exception:
                                                # If errno handling fails, fallback to conservative behavior
                                                pass
                                    else:
                                        # No pid parsed; if timestamp too old, attempt reclaim
                                        if existing_ts is not None and too_old:
                                            try:
                                                # As above, be conservative on network filesystems
                                                try:
                                                    is_nfs = _is_network_filesystem(
                                                        self.lock_path
                                                    )
                                                except Exception:
                                                    is_nfs = False
                                                allow_nfs = os.environ.get(
                                                    "TODO_ALLOW_STALE_RECLAIM_ON_NFS",
                                                    "",
                                                ).lower() in ("1", "true", "yes")
                                                if is_nfs and not allow_nfs:
                                                    logger.warning(
                                                        "Refusing to reclaim stale lockfile %s on network filesystem (no pid). Set TODO_ALLOW_STALE_RECLAIM_ON_NFS=1 to override",
                                                        self.lock_path,
                                                    )
                                                    pass
                                                else:
                                                    os.unlink(str(self.lock_path))
                                                    _inc_lock_metric("stale_reclaims")
                                                    logger.warning(
                                                        "Removed stale lockfile %s (no pid, too old)",
                                                        self.lock_path,
                                                    )
                                                    continue
                                            except Exception:
                                                _inc_lock_metric(
                                                    "stale_reclaim_failures"
                                                )
                                                logger.exception(
                                                    "Failed to remove stale lockfile %s",
                                                    self.lock_path,
                                                )
                                except PermissionError:
                                    # PID exists but cannot be signalled; treat as active
                                    pass
                            except Exception:
                                # Parsing diagnostics must not fail the lock acquisition
                                pass
                        except Exception:
                            logger.debug(
                                "Lockfile %s exists but could not be read",
                                self.lock_path,
                            )
                except Exception:
                    pass

                if time.time() - start >= self.timeout:
                    _inc_lock_metric("fallback_acquire_timeouts")
                    raise TimeoutError(f"Timeout acquiring lock {self.lock_path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fcntl is not None:
            try:
                # Only attempt to unlock if we have a valid file handle
                if self._fp is not None:
                    try:
                        self._fcntl.flock(self._fp.fileno(), self._fcntl.LOCK_UN)
                    except Exception:
                        # Best-effort: don't let unlocking fail the exit path
                        pass
            finally:
                try:
                    if self._fp is not None:
                        self._fp.close()
                except Exception:
                    pass
            return False

        # Fallback: close and remove lockfile
        try:
            if self._fd is not None:
                os.close(self._fd)
                _inc_lock_metric("fallback_releases")
                logger.debug(
                    "Closed fallback lock fd for %s (pid=%s)",
                    self.lock_path,
                    os.getpid(),
                )
        except Exception:
            pass
        try:
            os.unlink(str(self.lock_path))
            logger.debug("Unlinked fallback lockfile %s", self.lock_path)
        except FileNotFoundError:
            pass
        return False


def _load_todo_json(workdir: str) -> List[Dict[str, Any]]:
    p = _todo_json_path(workdir)
    # Read under the same advisory lock used for writes to avoid races.
    lockp = _lock_path(workdir)
    try:
        with _FileLock(lockp, timeout=2.0):
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    return []
            return []
    except TimeoutError:
        # If we cannot acquire the lock quickly, fall back to a best-effort read
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_todo(workdir: str, steps: List[Dict[str, Any]]) -> None:
    # Serialize JSON and Markdown to temp files and atomically replace the
    # canonical files while holding a per-workdir lock to prevent races.
    ctx_dir = agent_context_path(Path(workdir))
    ctx_dir.mkdir(parents=True, exist_ok=True)
    lockp = _lock_path(workdir)

    json_path = _todo_json_path(workdir)
    md_path = _todo_path(workdir)

    # Backup policy: keep this many recent backups per file
    _BACKUP_KEEP = 5

    def _prune_backups(target: Path, keep: int = _BACKUP_KEEP) -> None:
        try:
            pattern = f"{target.name}.bak.*"
            files = sorted(
                list(target.parent.glob(pattern)),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in files[keep:]:
                try:
                    old.unlink()
                except Exception:
                    logger.exception("Failed to remove old backup %s", old)
        except Exception:
            logger.exception("Failed to prune backups for %s", target)

    # Build markdown lines first (so failures creating JSON don't partially
    # update the human-readable file).
    _STATUS_ICONS = {
        "pending": "[ ]",
        "in_progress": "[~]",
        "blocked": "[!]",
        "done": "[x]",
        "verified": "[✓]",
    }
    lines = ["# Agent TODO\n"]
    for i, step in enumerate(steps):
        status = step.get("status", "done" if step.get("done", False) else "pending")
        checkbox = _STATUS_ICONS.get(status, "[ ]")
        desc = step.get("description", f"Step {i + 1}")
        deps = step.get("depends_on", [])
        blocked_reason = step.get("blocked_reason")

        suffix_parts = []
        if deps:
            dep_names = [f"Step {d + 1}" for d in deps]
            suffix_parts.append(f"depends on: {', '.join(dep_names)}")
        if blocked_reason:
            suffix_parts.append(f"blocked: {blocked_reason}")
        suffix = f" *({', '.join(suffix_parts)})*" if suffix_parts else ""

        lines.append(f"- {checkbox} **Step {i + 1}:** {desc}{suffix}")

    # Perform atomic writes under lock with backups and restore on failure
    with _FileLock(lockp, timeout=5.0):
        tmp_json = (
            json_path.parent
            / f"{json_path.name}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
        )
        tmp_md = (
            md_path.parent
            / f"{md_path.name}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
        )

        # Write temp files first
        try:
            with open(tmp_json, "w", encoding="utf-8") as f:
                json.dump(steps, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            with open(tmp_md, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
        except Exception:
            # Cleanup any partial temps
            try:
                if tmp_json.exists():
                    tmp_json.unlink()
            except Exception:
                pass
            try:
                if tmp_md.exists():
                    tmp_md.unlink()
            except Exception:
                pass
            raise

        # Move existing files to backups so we can restore on failure
        backups: Dict[Path, Path] = {}
        for target in (json_path, md_path):
            try:
                if target.exists():
                    ts = int(time.time() * 1000)
                    bak = target.parent / f"{target.name}.bak.{ts}"
                    # Use replace to atomically rename
                    target.replace(bak)
                    backups[target] = bak
            except Exception:
                logger.exception("Failed to create backup for %s", target)

        # Attempt to atomically replace temp files into place. If anything
        # fails, try to restore backed-up originals.
        try:
            tmp_json.replace(json_path)
            tmp_md.replace(md_path)
        except Exception:
            logger.exception("Failed to replace todo files; attempting restore")
            # Attempt restore from backups
            for orig, bak in backups.items():
                try:
                    if bak.exists():
                        bak.replace(orig)
                except Exception:
                    logger.exception("Failed to restore %s from %s", orig, bak)
            # Cleanup temps
            try:
                if tmp_json.exists():
                    tmp_json.unlink()
            except Exception:
                pass
            try:
                if tmp_md.exists():
                    tmp_md.unlink()
            except Exception:
                pass
            # Propagate error so callers can return structured errors
            raise

        # Success — prune old backups
        _prune_backups(json_path)
    _prune_backups(md_path)


def _get_orchestrator() -> Optional[Any]:
    """Best-effort: return the active orchestrator from the ContextVar, or None."""
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        return _PARENT_ORCHESTRATOR_VAR.get(None)
    except Exception:
        return None


def _notify_rbw_after_write(workdir: str) -> None:
    """Best-effort: update orchestrator RBW state and invalidate cached paths.

    This function should never raise; failures are non-critical and logged.
    """
    try:
        _inc_rbw_metric("rbw_notify_attempts")
        orch = _get_orchestrator()
        todo_md = _todo_path(workdir)
        todo_json = _todo_json_path(workdir)
        abs_md = str(todo_md.resolve())
        abs_json = str(todo_json.resolve())
        if orch is not None:
            try:
                if hasattr(orch, "_session_read_files"):
                    orch._session_read_files.add(abs_md)
                    orch._session_read_files.add(abs_json)
            except Exception:
                _inc_rbw_metric("rbw_notify_failures")
                logger.exception("Failed to update orchestrator._session_read_files")

        # Invalidate ContextBuilder caches for these paths if available
        try:
            from src.core.context.context_builder import ContextBuilder

            try:
                ContextBuilder.invalidate_path(abs_md)
            except Exception:
                # Fallback to clearing whole cache if targeted invalidation missing
                try:
                    ContextBuilder.clear_cache()
                except Exception:
                    _inc_rbw_metric("rbw_invalidate_failures")
                    logger.exception("ContextBuilder.clear_cache() failed")
        except Exception:
            # ContextBuilder not available — ignore but count as missing orchestrator path
            _inc_rbw_metric("rbw_missing_orch")
            logger.debug("ContextBuilder not available for RBW invalidation")
    except Exception:
        _inc_rbw_metric("rbw_notify_failures")
        logger.exception("_notify_rbw_after_write failed")


def notify_rbw(workdir: str, orchestrator: Optional[Any] = None) -> None:
    """Centralized, best-effort notification for RBW/session state.

    If an orchestrator instance is provided, update its _session_read_files
    directly (in-process safety-net). Otherwise fall back to the ContextVar
    lookup used by _notify_rbw_after_write.

    This helper never raises; failures are logged only.
    """
    try:
        todo_md = _todo_path(workdir)
        todo_json = _todo_json_path(workdir)
        abs_md = str(todo_md.resolve())
        abs_json = str(todo_json.resolve())

        if orchestrator is not None:
            try:
                _inc_rbw_metric("rbw_notify_attempts")
                if hasattr(orchestrator, "_session_read_files"):
                    orchestrator._session_read_files.add(abs_md)
                    orchestrator._session_read_files.add(abs_json)
            except Exception:
                _inc_rbw_metric("rbw_notify_failures")
                logger.exception(
                    "notify_rbw: failed to update orchestrator._session_read_files"
                )

            # Invalidate ContextBuilder caches for these paths if available
            try:
                from src.core.context.context_builder import ContextBuilder

                try:
                    ContextBuilder.invalidate_path(abs_md)
                except Exception:
                    try:
                        ContextBuilder.clear_cache()
                    except Exception:
                        _inc_rbw_metric("rbw_invalidate_failures")
                        logger.exception("ContextBuilder.clear_cache() failed")
            except Exception:
                _inc_rbw_metric("rbw_missing_orch")
                logger.debug("ContextBuilder not available for RBW invalidation")
            return

        # No explicit orchestrator provided — fall back to ContextVar-based notifier
        try:
            _notify_rbw_after_write(workdir)
        except Exception:
            logger.exception("notify_rbw: fallback _notify_rbw_after_write failed")
    except Exception:
        logger.exception("notify_rbw failed")


@tool(side_effects=["write"], tags=["planning"])
def manage_todo(
    action: str,
    workdir: str,
    description: Optional[str] = None,
    step_id: Optional[int] = None,
    steps: Optional[List[str]] = None,
    depends_on: Optional[List[List[int]]] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Manage the agent TODO list.

    DEP extension: Added depends_on parameter for DAG support.

    Actions:
      - "create": Create a new TODO with optional dependencies.
                  Pass: steps=["Step 1", "Step 2"],
                        depends_on=[None, [0]] (Step 2 depends on Step 0)
      - "check":  Mark a step as completed.
      - "update": Update a step's description.
      - "read":   Return the current TODO list.
      - "clear":  Remove the TODO list.
      - "start":  Mark a step as in_progress (only one can be active at a time).
      - "block":  Mark a step as blocked with an optional reason.
      - "verify": Mark a step as verified (human/test confirmed).
      - "next":   Return the next executable step (all dependencies done).

    Args:
        action: One of "create", "check", "update", "read", "clear", "start", "block", "verify", "next"
        workdir: Working directory (used to locate .agent-context/)
        description: New description for update action, or block reason
        step_id: 0-based step index for check/update/start/block/verify actions
        steps: List of step description strings for create action
        depends_on: List of dependency lists for each step (for create action)
        reason: Block reason for block action

    Returns:
        Dict with status and the current TODO state
    """
    valid_actions = {
        "create",
        "check",
        "update",
        "read",
        "clear",
        "start",
        "block",
        "verify",
        "next",
    }
    if action not in valid_actions:
        return {
            "status": "error",
            "error": f"Unknown action '{action}'. Use: {', '.join(sorted(valid_actions))}",
        }

    try:
        if action == "create":
            if not steps:
                return {
                    "status": "error",
                    "error": "steps list is required for create action",
                }

            # Validate and normalize depends_on into a list-of-lists of ints
            n = len(steps)
            normalized: List[List[int]] = []
            if depends_on is None:
                normalized = [[] for _ in range(n)]
            else:
                # depends_on must be a sequence (list/tuple) or None
                if not isinstance(depends_on, (list, tuple)):
                    return {
                        "status": "error",
                        "error": "depends_on must be a list of lists (or null)",
                    }
                for i in range(n):
                    if i < len(depends_on):
                        entry = depends_on[i]
                        if entry is None:
                            normalized.append([])
                            continue
                        if not isinstance(entry, (list, tuple)):
                            return {
                                "status": "error",
                                "error": f"depends_on[{i}] must be a list of integers or null",
                            }
                        deps_i: List[int] = []
                        for d in entry:
                            try:
                                di = int(d)
                            except Exception:
                                return {
                                    "status": "error",
                                    "error": f"depends_on[{i}] contains non-integer value: {d!r}",
                                }
                            if di < 0 or di >= n:
                                return {
                                    "status": "error",
                                    "error": f"depends_on[{i}] references out-of-range index {di} (0-{n - 1})",
                                }
                            deps_i.append(di)
                        normalized.append(deps_i)
                    else:
                        normalized.append([])

            # Detect cycles using Kahn's algorithm (edges: dep -> node)
            try:
                from collections import deque

                indeg = [0] * n
                adj: List[List[int]] = [[] for _ in range(n)]
                for i, deps in enumerate(normalized):
                    for d in deps:
                        adj[d].append(i)
                        indeg[i] += 1
                q = deque([i for i, v in enumerate(indeg) if v == 0])
                seen = 0
                while q:
                    u = q.popleft()
                    seen += 1
                    for v in adj[u]:
                        indeg[v] -= 1
                        if indeg[v] == 0:
                            q.append(v)
                if seen != n:
                    return {"status": "error", "error": "cycle detected in depends_on"}
            except Exception as e:
                logger.exception("manage_todo: dependency validation failed")
                return {"status": "error", "error": str(e)}

            # Build step dicts with normalized dependencies
            new_steps = []
            for i, s in enumerate(steps):
                step_dict: Dict[str, Any] = {"description": s, "done": False}
                step_dict["depends_on"] = normalized[i]
                new_steps.append(step_dict)

            _save_todo(workdir, new_steps)
            # Best-effort: update RBW/session state and invalidate caches
            try:
                _notify_rbw_after_write(workdir)
            except Exception:
                pass
            logger.info(f"manage_todo: created TODO with {len(new_steps)} steps")
            return {
                "status": "ok",
                "action": "created",
                "step_count": len(new_steps),
                "todo_path": str(_todo_path(workdir)),
                "steps": new_steps,
            }

        elif action == "check":
            if step_id is None:
                return {
                    "status": "error",
                    "error": "step_id is required for check action",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            if step_id < 0 or step_id >= len(current):
                return {
                    "status": "error",
                    "error": f"step_id {step_id} out of range (0-{len(current) - 1})",
                }
            # D-04: Idempotency guard — no-op if step is already done
            if current[step_id].get("status") == "done" or current[step_id].get("done"):
                done_count = sum(1 for s in current if s.get("done"))
                return {
                    "status": "no_change",
                    "action": "checked",
                    "step_id": step_id,
                    "done_count": done_count,
                    "total": len(current),
                    "steps": current,
                }
            current[step_id]["done"] = True
            current[step_id]["status"] = "done"
            _save_todo(workdir, current)
            try:
                _notify_rbw_after_write(workdir)
            except Exception:
                pass
            done_count = sum(1 for s in current if s.get("done"))
            logger.info(
                f"manage_todo: checked step {step_id} ({done_count}/{len(current)} done)"
            )
            # CP-5: when all steps are done, set verification_nudge_needed so the
            # agent is reminded to verify its work before declaring the task complete.
            all_done = done_count == len(current)
            result: Dict[str, Any] = {
                "status": "ok",
                "action": "checked",
                "step_id": step_id,
                "done_count": done_count,
                "total": len(current),
                "steps": current,
            }
            if all_done:
                result["verification_nudge_needed"] = True
                result["message"] = (
                    "All steps completed. Please verify your work before "
                    "declaring the task done."
                )
            return result

        elif action == "update":
            if step_id is None or description is None:
                return {
                    "status": "error",
                    "error": "step_id and description required for update",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            if step_id < 0 or step_id >= len(current):
                return {"status": "error", "error": f"step_id {step_id} out of range"}
            current[step_id]["description"] = description
            _save_todo(workdir, current)
            try:
                _notify_rbw_after_write(workdir)
            except Exception:
                pass
            return {
                "status": "ok",
                "action": "updated",
                "step_id": step_id,
                "steps": current,
            }

        elif action == "read":
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "ok",
                    "steps": [],
                    "message": "No TODO list exists yet",
                }
            done_count = sum(1 for s in current if s.get("done"))
            return {
                "status": "ok",
                "steps": current,
                "done_count": done_count,
                "total": len(current),
                "todo_path": str(_todo_path(workdir)),
            }

        elif action == "clear":
            p = _todo_path(workdir)
            jp = _todo_json_path(workdir)
            if p.exists():
                p.unlink()
            if jp.exists():
                jp.unlink()
            try:
                _notify_rbw_after_write(workdir)
            except Exception:
                pass
            logger.info("manage_todo: cleared TODO list")
            return {"status": "ok", "action": "cleared"}

        elif action == "start":
            if step_id is None:
                return {
                    "status": "error",
                    "error": "step_id is required for start action",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            if step_id < 0 or step_id >= len(current):
                return {
                    "status": "error",
                    "error": f"step_id {step_id} out of range (0-{len(current) - 1})",
                }
            # Only one step can be in_progress at a time
            active = [
                i for i, s in enumerate(current) if s.get("status") == "in_progress"
            ]
            if active and active[0] != step_id:
                return {
                    "status": "error",
                    "error": f"Step {active[0]} is already in_progress. Complete or block it first.",
                }
            # D-04: Idempotency guard — no-op if step is already in_progress
            if current[step_id].get("status") == "in_progress":
                return {
                    "status": "no_change",
                    "action": "started",
                    "step_id": step_id,
                    "steps": current,
                }
            current[step_id]["status"] = "in_progress"
            current[step_id]["started_at"] = (
                __import__("datetime").datetime.now().isoformat()
            )
            _save_todo(workdir, current)
            try:
                _notify_rbw_after_write(workdir)
            except Exception:
                pass
            return {
                "status": "ok",
                "action": "started",
                "step_id": step_id,
                "steps": current,
            }

        elif action == "block":
            if step_id is None:
                return {
                    "status": "error",
                    "error": "step_id is required for block action",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            if step_id < 0 or step_id >= len(current):
                return {
                    "status": "error",
                    "error": f"step_id {step_id} out of range (0-{len(current) - 1})",
                }
            current[step_id]["status"] = "blocked"
            current[step_id]["blocked_reason"] = description or reason or ""
            _save_todo(workdir, current)
            try:
                _notify_rbw_after_write(workdir)
            except Exception:
                pass
            return {
                "status": "ok",
                "action": "blocked",
                "step_id": step_id,
                "steps": current,
            }

        elif action == "verify":
            if step_id is None:
                return {
                    "status": "error",
                    "error": "step_id is required for verify action",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            if step_id < 0 or step_id >= len(current):
                return {
                    "status": "error",
                    "error": f"step_id {step_id} out of range (0-{len(current) - 1})",
                }
            # D-04: Idempotency guard — no-op if step is already verified
            if current[step_id].get("status") == "verified":
                done_count = sum(
                    1 for s in current if s.get("status") in ("done", "verified")
                )
                return {
                    "status": "no_change",
                    "action": "verified",
                    "step_id": step_id,
                    "done_count": done_count,
                    "total": len(current),
                    "steps": current,
                }
            current[step_id]["status"] = "verified"
            current[step_id]["completed_at"] = (
                __import__("datetime").datetime.now().isoformat()
            )
            _save_todo(workdir, current)
            try:
                _notify_rbw_after_write(workdir)
            except Exception:
                pass
            done_count = sum(
                1 for s in current if s.get("status") in ("done", "verified")
            )
            return {
                "status": "ok",
                "action": "verified",
                "step_id": step_id,
                "done_count": done_count,
                "total": len(current),
                "steps": current,
            }

        elif action == "next":
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "ok",
                    "next_step": None,
                    "message": "No TODO list found",
                }
            for i, step in enumerate(current):
                status = step.get("status", "pending")
                if status in ("done", "verified", "blocked"):
                    continue
                # Check if all dependencies are done
                deps = step.get("depends_on", [])
                all_deps_done = all(
                    current[d].get("status") in ("done", "verified")
                    for d in deps
                    if d < len(current)
                )
                if all_deps_done:
                    return {
                        "status": "ok",
                        "step_id": i,
                        "step": step,
                        "message": f"Next step: {step.get('description', '')}",
                    }
            return {
                "status": "ok",
                "next_step": None,
                "message": "All steps completed or blocked",
            }

        return {"status": "error", "error": f"Unhandled action '{action}'"}

    except Exception as e:
        logger.error(f"manage_todo: failed: {e}")
        return {"status": "error", "error": str(e)}
