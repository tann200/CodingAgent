"""
Guardrails module for file safety.

Provides read-before-write enforcement to prevent the agent from
writing to files it has never read, which would cause hallucinated
edits and corrupted files.
"""

import contextvars
import logging
import threading
from pathlib import Path
from typing import Dict, Set

logger = logging.getLogger(__name__)

# Two-level read tracking for correctness across threading models:
#
# 1. ContextVar — works correctly in same-context async chains and in Python 3.12+
#    run_in_executor (which copies context to executor threads).
# 2. Global session set (Lock-protected) — visible from ANY thread, ensuring that
#    a read_file call in one executor thread is always visible to a write_file call
#    in another executor thread (relevant in Python 3.11 where run_in_executor does
#    NOT propagate ContextVar state).
#
# Both are reset by reset_guardrail_state() on each new task.

_read_files_var: contextvars.ContextVar[Set[str]] = contextvars.ContextVar(
    "guardrail_read_files"
)
_global_read_files: Set[str] = set()
_global_lock = threading.Lock()


def _get_ctx_read_files() -> Set[str]:
    """Return the ContextVar-backed set, creating it if needed."""
    try:
        return _read_files_var.get()
    except LookupError:
        s: Set[str] = set()
        _read_files_var.set(s)
        return s


def mark_file_read(path: str) -> None:
    """Mark a file as read. Called by read_file/read_file_chunk on success.

    Updates both the ContextVar set and the global session set so that
    subsequent write calls from any thread can verify the read.

    Args:
        path: The resolved absolute path of the file that was read.
    """
    p = str(path)
    _get_ctx_read_files().add(p)
    with _global_lock:
        _global_read_files.add(p)


def check_read_before_write(path: str) -> Dict[str, str]:
    """Check that a file was read before allowing a write.

    Checks both the ContextVar set and the global session set so that
    cross-thread tool calls are handled correctly on Python 3.11+.

    Args:
        path: The resolved absolute path of the file to write.

    Returns:
        Empty dict if OK, or {"error": "...", "requires_read_first": True} on violation.
    """
    p = str(Path(path).resolve())
    in_context = p in _get_ctx_read_files()
    with _global_lock:
        in_global = p in _global_read_files
    # Allow new files (doesn't exist yet — no read possible)
    if Path(p).exists() and not (in_context or in_global):
        return {
            "error": (
                f"File '{p}' has not been read in this session. "
                "Read the file first (read_file) before making changes."
            ),
            "requires_read_first": True,
        }
    return {}


def reset_guardrail_state() -> None:
    """Reset read-tracking state. Called by Orchestrator.start_new_task()."""
    _read_files_var.set(set())
    with _global_lock:
        _global_read_files.clear()
