"""
File lock manager for Parallel Reads, Sequential Writes (PRSW).
Manages file-level locks for safe parallel access.

CRITICAL: All operations are async. NEVER use time.sleep() in async code.
"""

import asyncio
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

    def __init__(self, workdir: str, cancel_event: asyncio.Event = None):
        self.workdir = Path(workdir)
        self._cancel_event = cancel_event or asyncio.Event()
        self._lock_timeout = 30.0
        self._async_lock = asyncio.Lock()
        self._read_locks: Dict[str, Set[str]] = {}
        self._write_lock: Optional[FileLock] = None

    @classmethod
    def get_instance(
        cls, workdir: str = "", cancel_event: asyncio.Event = None
    ) -> "FileLockManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls(workdir=workdir or ".", cancel_event=cancel_event)
        return cls._instance

    def can_write(self, path: str) -> bool:
        """Check if file can be written (synchronous check)."""
        if self._write_lock and self._write_lock.path == path:
            return False
        if path in self._read_locks and self._read_locks[path]:
            return False
        return True

    def can_read(self, path: str) -> bool:
        """Check if file can be read (synchronous check)."""
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
        """
        timeout = timeout or self._lock_timeout
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
                    logger.info(
                        f"acquire_write_async: acquired for {path} by {agent_id}"
                    )
                    return True
            # Lock released — sleep without holding it so peers can proceed
            await asyncio.sleep(0.1)

    async def release_read(self, path: str, agent_id: str):
        """Release read lock (async)."""
        async with self._async_lock:
            if path in self._read_locks:
                self._read_locks[path].discard(agent_id)
                logger.debug(f"release_read: {agent_id} released read lock on {path}")

    async def release_write(self, path: str, agent_id: str):
        """Release write lock (async)."""
        async with self._async_lock:
            if self._write_lock and self._write_lock.path == path:
                if self._write_lock.locked_by == agent_id:
                    logger.info(f"release_write: released {path} from {agent_id}")
                    self._write_lock = None

    def cancel(self):
        """Signal cancellation to all waiting acquire operations."""
        self._cancel_event.set()

    def reset_cancel(self):
        """Reset cancellation signal for new operation."""
        self._cancel_event.clear()

    def get_lock_status(self, path: str) -> Dict:
        """Get lock status for a file."""
        status = {"path": path, "readers": [], "writer": None}

        if path in self._read_locks:
            status["readers"] = list(self._read_locks[path])

        if self._write_lock and self._write_lock.path == path:
            status["writer"] = self._write_lock.locked_by

        return status


def get_file_lock_manager(
    workdir: str = "", cancel_event: asyncio.Event = None
) -> FileLockManager:
    """Get the global file lock manager instance."""
    return FileLockManager.get_instance(workdir=workdir, cancel_event=cancel_event)
