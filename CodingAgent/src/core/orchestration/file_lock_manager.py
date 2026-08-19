"""
File lock manager for Parallel Reads, Sequential Writes (PRSW).
Manages file-level locks for safe parallel access.

CRITICAL: All operations are async. NEVER use time.sleep() in async code.
"""

import asyncio
import atexit
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class FileLock:
    path: str
    locked_by: str
    lock_type: str
    timestamp: float


class FileLockManager:
    """
    Manages file-level locks for safe parallel access.

    - Multiple agents can hold READ locks on the same file
    - Only ONE agent can hold a WRITE lock on a file
    - WRITE lock excludes ALL other locks (read or write)

    CRITICAL: Uses asyncio.Lock for thread-safe async operations.
    All methods that modify state are async and use await.
    """

    _instance = None
    _instance_lock = threading.Lock()
    _atexit_installed = False

    def __init__(self, workdir: str, cancel_event: Optional[asyncio.Event] = None):
        self.workdir = Path(workdir)
        self._cancel_event = cancel_event or asyncio.Event()
        self._cancel_owner: Optional[str] = None
        self._lock_timeout = 30.0
        self._async_lock = asyncio.Lock()
        self._read_locks: Dict[str, Set[str]] = {}
        self._write_lock: Optional[FileLock] = None
        # Guard task reference for zombie-lock auto-release
        self._lock_release_guard: Optional[asyncio.Task] = None
        self._install_atexit_cleanup()

    @classmethod
    def get_instance(
        cls, workdir: str = "", cancel_event: Optional[asyncio.Event] = None
    ) -> "FileLockManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls(
                        workdir=workdir or ".", cancel_event=cancel_event
                    )
        return cls._instance

    @classmethod
    def reset_instance(
        cls, workdir: str = "", cancel_event: Optional[asyncio.Event] = None
    ) -> "FileLockManager":
        """Replace the singleton with a fresh instance.

        Cancels all pending lock operations on the old instance and creates
        a clean replacement.  Safe to call on orchestrator re-initialization.
        """
        with cls._instance_lock:
            old = cls._instance
            if old is not None:
                old.cancel()
            cls._instance = cls(
                workdir=workdir or ".", cancel_event=cancel_event
            )
        if old is not None:
            # Allow pending operations to observe the cancel signal
            time.sleep(0.05)
        return cls._instance

    @classmethod
    def _install_atexit_cleanup(cls):
        """Register an atexit handler that releases all held locks on process exit."""
        if cls._atexit_installed:
            return
        cls._atexit_installed = True

        def _release_all():
            inst = cls._instance
            if inst is not None:
                # Logging streams can already be closed during interpreter
                # teardown (including pytest's capture stream), so cleanup
                # must be silent and best-effort.
                inst._write_lock = None
                inst._read_locks.clear()

        atexit.register(_release_all)

    def can_write(self, path: str) -> bool:
        """Check if file can be written (synchronous advisory check).

        .. warning::
            This is a **non-atomic, advisory** check.  It reads shared lock state
            without holding ``_async_lock``, so a concurrent async
            ``acquire_write_lock()`` / ``acquire_read_lock()`` call can race against
            this read.  Do not rely on this result for correctness-critical decisions;
            use the async acquire methods for that.
        """
        if self._write_lock and self._write_lock.path == path:
            return False
        if path in self._read_locks and self._read_locks[path]:
            return False
        return True

    def can_read(self, path: str) -> bool:
        """Check if file can be read (synchronous advisory check).

        .. warning::
            Same advisory / non-atomic caveat as :meth:`can_write`.
        """
        if self._write_lock and self._write_lock.path == path:
            return False
        return True

    async def acquire_read_async(self, path: str, agent_id: str) -> bool:
        """Acquire read lock. Multiple agents can read same file."""
        async with self._async_lock:
            if self._write_lock and self._write_lock.path == path:
                return False

            if path not in self._read_locks:
                self._read_locks[path] = set()
            self._read_locks[path].add(agent_id)
            logger.debug(f"acquire_read_async: {agent_id} acquired read lock on {path}")
            return True

    async def acquire_write_async(
        self, path: str, agent_id: str, timeout: Optional[float] = None
    ) -> bool:
        """
        Async write lock acquisition with cancellation and timeout.

        CRITICAL: This is async. Use ONLY await with this method.
        NEVER call this synchronously - it will deadlock.

        When the lock is acquired, a background guard task is started that
        will auto-release the lock after *timeout* seconds as a safety net
        against hanging tool executions.
        """
        timeout = self._lock_timeout if timeout is None else timeout
        start = time.time()

        # Poll without holding _async_lock during the sleep so other lock
        # operations (acquire_read, release_*) are not blocked for the full
        # backoff duration.  The lock is only held during the short critical
        # section that inspects and sets state.
        while True:
            if self._cancel_event.is_set():
                logger.warning(f"acquire_write_async: cancelled for {path}")
                return False

            elapsed = time.time() - start
            if elapsed > timeout:
                logger.error(
                    f"acquire_write_async: timeout after {elapsed:.1f}s for {path}"
                )
                return False

            async with self._async_lock:
                if self._cancel_event.is_set():
                    return False
                if self.can_write(path):
                    self._write_lock = FileLock(path, agent_id, "write", time.time())
                    # Start auto-release guard for zombie-lock prevention
                    self._lock_release_guard = asyncio.ensure_future(
                        self._auto_release_after_timeout(path, agent_id, timeout)
                    )
                    logger.info(
                        f"acquire_write_async: acquired for {path} by {agent_id}"
                    )
                    return True
            # Lock released — sleep without holding it so peers can proceed
            await asyncio.sleep(0.1)

    async def _auto_release_after_timeout(self, path: str, agent_id: str, timeout_sec: float) -> None:
        """Auto-release the write lock if the holder does not release it within
        *timeout_sec*.

        This is a safety net against zombie locks caused by hanging tool
        executor threads or unhandled CancelledError paths.  The release
        fires at *timeout_sec* after acquisition, regardless of whether the
        tool is still running.
        """
        await asyncio.sleep(timeout_sec)
        async with self._async_lock:
            if self._write_lock and self._write_lock.path == path:
                if self._write_lock.locked_by == agent_id:
                    logger.critical(
                        "AUTO-RELEASING zombie write lock for %s (agent=%s, timeout=%.1fs) — "
                        "tool execution may have hung",
                        path, agent_id, timeout_sec,
                    )
                    self._write_lock = None

    async def release_read(self, path: str, agent_id: str):
        """Release read lock (async)."""
        async with self._async_lock:
            if path in self._read_locks:
                self._read_locks[path].discard(agent_id)
                logger.debug(f"release_read: {agent_id} released read lock on {path}")

    async def release_write(self, path: str, agent_id: str):
        """Release write lock (async).

        Cancels the auto-release guard task if running, so the normal
        release path does not race with the timeout-based fallback.
        """
        if self._lock_release_guard is not None and not self._lock_release_guard.done():
            self._lock_release_guard.cancel()
            self._lock_release_guard = None
        async with self._async_lock:
            if self._write_lock and self._write_lock.path == path:
                if self._write_lock.locked_by == agent_id:
                    logger.info(f"release_write: released {path} from {agent_id}")
                    self._write_lock = None

    def cancel(self, owner: Optional[str] = None):
        """Signal cancellation to all waiting acquire operations.

        When *owner* is set, only operations matching that owner can
        clear this cancel signal via ``reset_cancel()``.
        """
        self._cancel_event.set()
        if owner is not None:
            self._cancel_owner = owner

    def reset_cancel(self, owner: Optional[str] = None):
        """Reset cancellation signal for new operation.

        Only clears the event when *owner* matches the original
        ``cancel()`` caller.  This prevents one coroutine from
        inadvertently clearing a cancel signal set by another.
        """
        if self._cancel_owner is not None:
            if owner is None or owner != self._cancel_owner:
                return
        self._cancel_event.clear()
        self._cancel_owner = None

    def get_lock_status(self, path: str) -> Dict:
        """Get lock status for a file."""
        status: Dict[str, object] = {"path": path, "readers": [], "writer": None}

        if path in self._read_locks:
            status["readers"] = list(self._read_locks[path])

        if self._write_lock and self._write_lock.path == path:
            status["writer"] = self._write_lock.locked_by

        return status


def get_file_lock_manager(
    workdir: str = "", cancel_event: Optional[asyncio.Event] = None
) -> FileLockManager:
    """Get the global file lock manager instance."""
    return FileLockManager.get_instance(workdir=workdir, cancel_event=cancel_event)
