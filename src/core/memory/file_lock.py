"""file_lock.py — OS-level file locking for memory files.

TASK-REL-2: Adds exclusive/shared locking so concurrent processes or async
tasks cannot interleave writes to memory.md (and the future user.md).

Design
------
* One ``threading.Lock`` per resolved path (held in a module-level registry)
  guards inter-*thread* access within the same process.
* ``fcntl.flock`` (Unix) or a ``threading.Lock`` fallback (Windows, where
  ``fcntl`` is unavailable) guards inter-*process* access.
* ``locked_file()`` is a context manager that:
    1. Acquires the per-path threading lock.
    2. Opens the file (creating it if necessary for write modes).
    3. Acquires the OS-level lock (exclusive for writes, shared for reads).
    4. Yields the open file handle.
    5. Releases the OS lock, closes the file, then releases the threading lock.

Usage::

    from src.core.memory.file_lock import locked_file

    # Exclusive write
    with locked_file(memory_path, mode="r+") as fh:
        old = fh.read()
        fh.seek(0)
        fh.write(old + new_entry)
        fh.truncate()

    # Shared read (multiple readers allowed simultaneously)
    with locked_file(memory_path, mode="r") as fh:
        content = fh.read()

    # Write to a file that may not exist yet
    with locked_file(memory_path, mode="a") as fh:
        fh.write(new_entry)
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Generator

# ---------------------------------------------------------------------------
# Per-path threading lock registry
# ---------------------------------------------------------------------------

_registry_lock: threading.Lock = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def _get_path_lock(path: Path) -> threading.Lock:
    """Return (creating if needed) the per-resolved-path threading lock."""
    key = str(path.resolve())
    with _registry_lock:
        if key not in _path_locks:
            _path_locks[key] = threading.Lock()
        return _path_locks[key]


# ---------------------------------------------------------------------------
# OS-level locking helpers
# ---------------------------------------------------------------------------

try:
    import fcntl as _fcntl

    def _lock_file(fh: IO, exclusive: bool) -> None:
        _fcntl.flock(
            fh.fileno(),
            _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH,
        )

    def _unlock_file(fh: IO) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)

except ImportError:
    # Windows: fcntl is unavailable.
    # Try to use msvcrt for basic file locking on Windows.
    # If that fails, log a warning and continue with threading.Lock only.
    try:
        import msvcrt

        def _lock_file(fh: IO, exclusive: bool) -> None:
            # msvcrt.locking provides basic file locking
            # Note: This is per-process, not per-file-handle
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            except (OSError, ImportError):
                # Locking failed or not available - continue with threading lock only
                pass

        def _unlock_file(fh: IO) -> None:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, ImportError):
                pass

    except ImportError:
        # Neither fcntl nor msvcrt available - no-op fallback
        import logging as _logging

        _logger = _logging.getLogger(__name__)
        _warned = False

        def _lock_file(fh: IO, exclusive: bool) -> None:  # type: ignore[misc]
            global _warned
            if not _warned:
                _logger.warning(
                    "file_lock: No OS-level locking available (fcntl/msvcrt unavailable). "
                    "Using threading.Lock only - cross-process safety not guaranteed."
                )
                _warned = True

        def _unlock_file(fh: IO) -> None:  # type: ignore[misc]
            pass


# ---------------------------------------------------------------------------
# Public context manager
# ---------------------------------------------------------------------------


@contextmanager
def locked_file(
    path: Path,
    mode: str = "r",
    encoding: str = "utf-8",
) -> Generator[IO, None, None]:
    """Open *path* with OS-level locking and yield the file handle.

    Parameters
    ----------
    path:
        Filesystem path to lock and open.
    mode:
        Standard ``open()`` mode string.  Write/append modes (contains ``"w"``,
        ``"a"``, or ``"+"``) acquire an *exclusive* lock; read-only modes
        acquire a *shared* lock.
    encoding:
        Text encoding passed to ``open()``.  Ignored for binary modes.

    Yields
    ------
    IO
        Open file handle (text or binary depending on mode).
    """
    exclusive = any(c in mode for c in ("w", "a", "+"))
    thread_lock = _get_path_lock(path)

    with thread_lock:
        # Ensure parent directory exists for write/append modes.
        if exclusive:
            path.parent.mkdir(parents=True, exist_ok=True)

        # For read modes the file must already exist; callers should check.
        open_mode = mode
        if "b" in mode:
            fh: IO = open(path, open_mode)
        else:
            fh = open(path, open_mode, encoding=encoding)

        try:
            _lock_file(fh, exclusive)
            try:
                yield fh
            finally:
                _unlock_file(fh)
        finally:
            fh.close()
