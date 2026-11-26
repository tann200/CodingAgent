"""Canonical SessionStore facade backed exclusively by SQLite (WAL + FTS5).

TASK-24: Standardized on ``SqliteSessionStore`` as the sole canonical session
backend, retiring the 1,304-LOC ``JsonlSessionStore`` backend and dual-backend
shim.  Portable JSONL import/export is available via ``jsonl_io`` (``export_jsonl``
and ``import_jsonl``).
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Optional

from src.core.memory.jsonl_io import export_session_jsonl, import_session_jsonl
from src.core.memory.sqlite_session_store import SqliteSessionStore

logger = logging.getLogger(__name__)


def _resolve_backend(explicit: Optional[str] = None) -> str:
    """Return ``'sqlite'`` (the sole canonical storage backend)."""
    if explicit and str(explicit).lower() == "jsonl":
        logger.debug(
            "session_store: legacy 'jsonl' backend retired in TASK-24; using canonical SQLite store"
        )
    return "sqlite"


def _create_backend(
    workdir: Optional[str] = None, backend: Optional[str] = None
) -> SqliteSessionStore:
    """Create and return the canonical ``SqliteSessionStore`` instance."""
    _resolve_backend(backend)
    return SqliteSessionStore(workdir)


def get_session_store(
    workdir: Optional[str] = None,
    backend: Optional[str] = None,
    *,
    private: bool = False,
) -> Any:
    """Return the canonical SQLite-backed session store (or ``NullSessionStore`` when *private* is True).

    When *backend* is passed explicitly, returns the raw ``SqliteSessionStore``.
    Otherwise returns a ``SessionStore`` wrapper that normalises falsy/None
    ``session_id`` arguments to ``"unknown"`` for backward compatibility.
    """
    if private:
        return NullSessionStore(workdir)
    if backend is not None:
        return _create_backend(workdir, backend)
    return SessionStore(workdir)


class NullSessionStore:
    """Ephemeral no-op SessionStore for ``--private`` sessions (TASK-36).

    Satisfies the SessionStore interface in memory without creating any SQLite
    databases, WAL files, or `.codingAgent/` artifacts on disk.
    """

    _SCHEMA_VERSION = SqliteSessionStore._SCHEMA_VERSION

    def __init__(self, workdir: Optional[str] = None) -> None:
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self._workdir = self.workdir
        self._db_path = None
        self.private = True

    def get_schema_version(self) -> int:
        return int(self._SCHEMA_VERSION)

    def add_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_tool_call(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_plan(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_error(self, *args: Any, **kwargs: Any) -> None:
        return None

    def save_session(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_messages(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def list_sessions(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop


class SessionStore:
    """Thin compatibility facade over ``SqliteSessionStore``.

    Normalises falsy/None ``session_id`` arguments to ``"unknown"`` and exposes
    portable JSONL import/export helpers (``export_jsonl``, ``import_jsonl``).
    """

    _SCHEMA_VERSION = SqliteSessionStore._SCHEMA_VERSION

    def __init__(
        self, workdir: Optional[str] = None, backend: Optional[str] = None
    ) -> None:
        self._store = _create_backend(workdir, backend)
        self._workdir = Path(workdir) if workdir else self._store.workdir
        self._db_path = self._store._cm.resolve_db_path()
        self._agent_context_dir = self._store._cm._agent_context_dir
        self._SCHEMA_VERSION = getattr(
            self._store, "_SCHEMA_VERSION", self._SCHEMA_VERSION
        )

    def get_schema_version(self) -> int:
        try:
            return int(self._store.get_schema_version())
        except Exception:
            return int(self._SCHEMA_VERSION)

    def export_jsonl(self, session_id: str, dest: Path) -> int:
        """Export *session_id* to a portable redacted JSONL file at *dest*."""
        return export_session_jsonl(self._store, session_id or "unknown", dest)

    def import_jsonl(
        self, src: Path, *, session_id: Optional[str] = None
    ) -> str:
        """Import a JSONL export file into this SQLite session store."""
        return import_session_jsonl(self._store, src, session_id=session_id)

    def close(self) -> None:
        self._store.close()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._store, name)
        if not callable(attr):
            return attr

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            if "session_id" in kwargs and not kwargs.get("session_id"):
                kwargs["session_id"] = "unknown"
            try:
                params = list(inspect.signature(attr).parameters.keys())
                if params and params[0] == "session_id" and args and not args[0]:
                    args = ("unknown",) + args[1:]
            except Exception:
                pass
            return attr(*args, **kwargs)

        return _wrapped


__all__ = [
    "NullSessionStore",
    "SessionStore",
    "SqliteSessionStore",
    "_create_backend",
    "_resolve_backend",
    "export_session_jsonl",
    "get_session_store",
    "import_session_jsonl",
]
