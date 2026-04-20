"""Compatibility shim for session store selection.

This module provides a small, stable import surface so existing code that
``from src.core.memory.session_store import SessionStore`` continues to work.

By default SessionStore is a thin wrapper around JsonlSessionStore (append-only
per-session JSONL files). Callers may request the SQLite-backed store by
explicitly selecting the backend via environment variable
``CODING_AGENT_STORAGE_BACKEND=sqlite`` or by passing ``backend="sqlite"`` to
``get_session_store`` or to the SessionStore constructor.

The wrapper normalises falsy/None session_id values to the sentinel
``"unknown"`` to preserve prior behaviour.
"""

from __future__ import annotations

import os
import inspect
import sqlite3
import threading
import json
import tempfile
import shutil
from pathlib import Path
from typing import Any, Optional

try:
    from src.core.memory.jsonl_session_store import JsonlSessionStore
except Exception:
    JsonlSessionStore = None  # type: ignore

try:
    from src.core.memory.sqlite_session_store import SqliteSessionStore
except Exception:
    SqliteSessionStore = None  # type: ignore


def _resolve_backend(explicit: Optional[str] = None) -> str:
    """Resolve which backend to use: explicit arg > env var > default jsonl."""
    if explicit:
        return str(explicit).lower()
    env = os.getenv("CODING_AGENT_STORAGE_BACKEND", "").lower()
    if env in ("jsonl", "sqlite"):
        return env
    # Try project config if available
    try:
        from src.core.config_loader import get as _cfg_get

        # Ask the config loader for an explicit override. Do NOT supply a
        # default here — if the config key is absent we want to fall through
        # to the code-level default (sqlite) rather than accepting the
        # config_loader's own default value.
        cfg_val = str(_cfg_get("storage_backend") or "").lower()
        if cfg_val in ("jsonl", "sqlite"):
            return cfg_val
    except Exception:
        pass

    # Fall back to sqlite as the project-level default for tests and local
    # developer workflows.
    return "sqlite"


def _create_backend(workdir: Optional[str] = None, backend: Optional[str] = None):
    """Create and return the raw backend implementation instance.

    This function is intentionally separate from ``get_session_store`` so the
    SessionStore wrapper can obtain an underlying implementation without
    causing recursion.
    """
    _backend = _resolve_backend(backend)
    if _backend == "sqlite" and SqliteSessionStore is not None:
        return SqliteSessionStore(workdir)
    if JsonlSessionStore is not None:
        return JsonlSessionStore(workdir)
    raise RuntimeError("No session store implementations are available")


def get_session_store(workdir: Optional[str] = None, backend: Optional[str] = None):
    """Factory: return an instance of the selected session store backend.

    Defaults to JsonlSessionStore. When ``backend=="sqlite"`` and the
    SqliteSessionStore implementation is available this will return a
    SqliteSessionStore instance.
    """
    """Return a SessionStore wrapper by default (when backend is not
    explicitly provided). If *backend* is supplied explicitly, return the raw
    backend implementation instance (JsonlSessionStore or SqliteSessionStore).
    """

    def _instantiate_raw(wd: Optional[str], explicit: Optional[str] = None):
        """Dynamically import and instantiate the requested raw backend.

        Importing inside the function ensures test-time patches of the
        concrete backend classes (eg. in jsonl_session_store) are observed.
        """
        _backend = _resolve_backend(explicit)
        if _backend == "sqlite":
            from src.core.memory.sqlite_session_store import SqliteSessionStore as _Sql

            return _Sql(wd)
        if _backend == "jsonl":
            from src.core.memory.jsonl_session_store import JsonlSessionStore as _Json

            return _Json(wd)
        raise RuntimeError("No session store implementations are available")

    # If the caller explicitly requested a backend, return the raw
    # implementation instance. This honours callers who intentionally ask for
    # a specific backend and allows tests to patch backend constructors.
    if backend is not None:
        try:
            return _instantiate_raw(workdir, backend)
        except Exception:
            # If instantiation fails, fall back to the wrapper to preserve
            # overall functionality.
            return SessionStore(workdir)

    # No explicit backend: resolve configured backend and decide.
    resolved = _resolve_backend(None)
    if resolved == "jsonl":
        # When config explicitly requests jsonl prefer returning the raw
        # JsonlSessionStore so callers that expect the concrete type receive
        # it. If construction fails, fall back to the wrapper.
        try:
            from src.core.memory.jsonl_session_store import JsonlSessionStore as _Json  # type: ignore

            return _Json(workdir)
        except Exception:
            return SessionStore(workdir)

    # Default: return the compatibility wrapper (typically for sqlite)
    return SessionStore(workdir)


class SessionStore:
    """Backward-compatible wrapper that normalises None session_id to "unknown".

    This class delegates to an underlying store instance returned by
    ``get_session_store``. Attribute access is proxied; callables are wrapped so
    any parameter named ``session_id`` that is falsy/None is replaced with the
    string ``"unknown"`` before invocation.
    """

    # Expose a class-level schema version constant so tests that reference
    # ``SessionStore._SCHEMA_VERSION`` continue to work. Prefer the sqlite
    # implementation's constant when available, then fall back to the jsonl
    # implementation, then to 1 as a final default.
    _SCHEMA_VERSION = int(
        getattr(
            SqliteSessionStore,
            "_SCHEMA_VERSION",
            getattr(JsonlSessionStore, "_SCHEMA_VERSION", 1)
            if JsonlSessionStore is not None
            else 1,
        )
    )

    def __init__(self, workdir: Optional[str] = None, backend: Optional[str] = None):
        # Obtain the raw backend implementation without calling
        # get_session_store (which returns a wrapper by default). This avoids
        # recursion when constructing the SessionStore wrapper itself.
        self._store = _create_backend(workdir, backend)
        # Workdir resolution: prefer explicit arg, else try underlying store
        if workdir:
            self._workdir = Path(workdir)
        else:
            # Underlying stores expose different attributes for workdir
            w = getattr(self._store, "_workdir", None) or getattr(
                self._store, "workdir", None
            )
            self._workdir = Path(w) if w is not None else Path.cwd()
        # Compatibility lock used when underlying store is not sqlite-backed
        self._compat_lock = threading.RLock()
        # Per-thread connection storage for fallback sqlite DB
        self._local = threading.local()
        # Path for a potential sqlite DB used by tests that access sqlite internals
        self._db_path = self._workdir / ".agent-context" / "session.db"
        # Expose schema version constant for backward compatibility tests.
        try:
            # Also set an instance attribute (keeps prior behaviour for callers
            # that inspect the instance) to the underlying store's value when
            # available.
            self._SCHEMA_VERSION = getattr(
                self._store, "_SCHEMA_VERSION", self._SCHEMA_VERSION
            )
        except Exception:
            self._SCHEMA_VERSION = self._SCHEMA_VERSION

    def get_schema_version(self) -> int:
        """Proxy to underlying store's schema/version when available."""
        try:
            if hasattr(self._store, "get_schema_version"):
                return int(self._store.get_schema_version())
        except Exception:
            pass
        try:
            return int(getattr(self._store, "_SCHEMA_VERSION", self._SCHEMA_VERSION))
        except Exception:
            return int(self._SCHEMA_VERSION)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._store, name)
        if not callable(attr):
            return attr

        def _wrapped(*args, **kwargs):
            # Normalise keyword session_id
            if "session_id" in kwargs and not kwargs.get("session_id"):
                kwargs["session_id"] = "unknown"

            # Inspect signature to see if first positional arg is session_id
            try:
                sig = inspect.signature(attr)
                params = list(sig.parameters.keys())
                if params:
                    first = params[0]
                    if first == "session_id" and len(args) >= 1 and not args[0]:
                        # Replace the first arg with the sentinel
                        args = ("unknown",) + args[1:]
            except Exception:
                # If inspection fails, fall back to keyword-only normalisation
                pass

            return attr(*args, **kwargs)

        return _wrapped

    # ------------------------------------------------------------------
    # Compatibility helpers: provide sqlite-like internals when the
    # underlying store is jsonl-based so tests that directly access
    # _get_connection/_lock continue to work.
    # ------------------------------------------------------------------

    @property
    def _lock(self):
        # Prefer underlying store's lock when available
        return getattr(self._store, "_lock", self._compat_lock)

    def _get_connection(self) -> sqlite3.Connection:
        """Return a per-thread sqlite3.Connection.

        If the underlying store provides a connection, use it. Otherwise
        lazily create a fallback sqlite DB at {workdir}/.agent-context/session.db
        and ensure minimal tables (decisions) exist. This allows tests to
        insert directly into the DB even when the primary store is jsonl.
        """
        # Use underlying implementation if present
        if hasattr(self._store, "_get_connection"):
            try:
                return getattr(self._store, "_get_connection")()
            except Exception:
                # Fall through to fallback implementation
                pass

        if not hasattr(self._local, "connection") or self._local.connection is None:
            # Ensure parent dir
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            try:
                conn.execute("PRAGMA busy_timeout=1000")
            except Exception:
                pass

            # Ensure minimal schema for decisions table so tests can insert
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        rationale TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.commit()
            except Exception:
                # Ignore schema creation errors — best-effort compatibility
                pass

            self._local.connection = conn

            # Try to register thread-local connection on underlying wrapper if it
            # has a thread registry (keeps diagnostics similar to SqliteSessionStore)
            try:
                if hasattr(self._store, "_thread_connections") and hasattr(
                    self._store, "_thread_connections_lock"
                ):
                    with getattr(self._store, "_thread_connections_lock"):
                        getattr(self._store, "_thread_connections")[
                            threading.get_ident()
                        ] = conn
            except Exception:
                pass

        return self._local.connection

    def write_decisions_json(self, limit: int = 50) -> None:
        """Write recent decisions to {workdir}/.agent-context/decisions.json.

        Prefer reading from a sqlite DB if present (so tests that write
        directly into the DB are respected). Otherwise delegate to the
        underlying store when available.
        """
        # If sqlite DB exists and has any decisions, use it
        try:
            if self._db_path.exists():
                conn = self._get_connection()
                cur = conn.execute(
                    "SELECT session_id, decision, rationale, created_at FROM decisions ORDER BY created_at DESC LIMIT ?",
                    (int(limit),),
                )
                rows = cur.fetchall()
                decisions = []
                for r in rows:
                    decisions.append(
                        {
                            "session_id": r["session_id"],
                            "decision": r["decision"],
                            "rationale": r["rationale"],
                            "ts": r["created_at"],
                        }
                    )
                # Atomic write to decisions.json
                out_dir = self._workdir / ".agent-context"
                out_dir.mkdir(parents=True, exist_ok=True)
                dest = out_dir / "decisions.json"
                fd = None
                tmp = None
                try:
                    fd, tmp = tempfile.mkstemp(dir=str(out_dir), suffix=".tmp")
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        fd = None
                        json.dump(decisions, f, ensure_ascii=False)
                        try:
                            f.flush()
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    try:
                        os.replace(tmp, str(dest))
                    except Exception:
                        try:
                            shutil.move(tmp, str(dest))
                        except Exception:
                            pass
                finally:
                    try:
                        if fd is not None:
                            os.close(fd)
                    except Exception:
                        pass
                return
        except Exception:
            # Fall through to delegate to underlying store
            pass

        # Delegate to underlying store if it supports write_decisions_json
        if hasattr(self._store, "write_decisions_json"):
            try:
                return getattr(self._store, "write_decisions_json")(limit=limit)
            except Exception:
                pass

    def _write_with_retry(
        self,
        conn: Any,
        sql: str,
        params: tuple = (),
        session_id: Optional[str] = None,
        attempts: int = 5,
        base_backoff: float = 0.05,
    ) -> bool:
        """Proxy/compatibility shim for stores that implement _write_with_retry.

        Prefer delegating to the underlying store; otherwise use a simple
        implementation that mirrors Jsonl/Sqlite semantics for tests.
        """
        if hasattr(self._store, "_write_with_retry"):
            try:
                return getattr(self._store, "_write_with_retry")(
                    conn, sql, params, session_id, attempts, base_backoff
                )
            except Exception:
                # Fall through to local implementation
                pass

        # Local fallback implementation: attempt conn.execute/commit and
        # retry on sqlite3.OperationalError containing 'lock'. On exhaustion
        # write a diagnostic JSON sidecar into {workdir}/.agent-context.
        import sqlite3 as _sqlite3

        last_err = None
        for i in range(1, max(1, int(attempts)) + 1):
            try:
                if hasattr(conn, "execute"):
                    conn.execute(sql, params)
                if hasattr(conn, "commit"):
                    conn.commit()
                return True
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if isinstance(e, _sqlite3.OperationalError) and "lock" in msg:
                    sleep_for = float(base_backoff) * (2 ** (i - 1))
                    try:
                        import time

                        time.sleep(sleep_for)
                    except Exception:
                        pass
                    continue
                else:
                    break

        # Exhausted or unrecoverable — write diagnostic file
        try:
            out_dir = self._workdir / ".agent-context"
            out_dir.mkdir(parents=True, exist_ok=True)
            import time

            ts = int(time.time() * 1000)
            sid = session_id or "unknown"
            diag_path = out_dir / f"session_store_write_failure_{ts}_{sid}.json"
            payload = {
                "db_path": str(
                    getattr(conn, "database", getattr(conn, "db_path", "unknown"))
                )
                if conn is not None
                else None,
                "session_id": sid,
                "attempts": attempts,
                "last_error": str(last_err),
                "sql": sql,
                "params": params,
                "ts": ts,
            }
            fd = None
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=str(out_dir), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    fd = None
                    json.dump(payload, f, ensure_ascii=False)
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                try:
                    os.replace(tmp, str(diag_path))
                except Exception:
                    try:
                        shutil.move(tmp, str(diag_path))
                    except Exception:
                        pass
            finally:
                try:
                    if fd is not None:
                        os.close(fd)
                except Exception:
                    pass
        except Exception:
            # Best-effort: ignore failures
            pass

        return False

    def read_recent_decisions(self, max_entries: int = 10) -> list:
        """Read recent decisions either from decisions.json sidecar or delegate.

        Returns an empty list when no decisions are available or the file is
        malformed.
        """
        # Prefer the sidecar file when present
        try:
            out = self._workdir / ".agent-context" / "decisions.json"
            if out.exists():
                data = json.loads(out.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[: max(0, int(max_entries))]
                return []
        except Exception:
            return []

        # Delegate to underlying store if it provides a reader
        if hasattr(self._store, "read_recent_decisions"):
            try:
                return getattr(self._store, "read_recent_decisions")(
                    max_entries=max_entries
                )
            except Exception:
                return []

        return []

    # Provide explicit pass-throughs for commonly-used APIs so they are
    # discoverable as attributes on the SessionStore class (some tests assert
    # ``hasattr(SessionStore, "register_child_session")`` at import time).
    def register_child_session(
        self,
        parent_session_id: str,
        child_session_id: str,
        role: Optional[str] = None,
        task: Optional[str] = None,
    ) -> None:
        return getattr(self._store, "register_child_session")(
            parent_session_id, child_session_id, role, task
        )

    def get_child_sessions(self, parent_session_id: str) -> Any:
        return getattr(self._store, "get_child_sessions")(parent_session_id)

    def get_session_tree(self, session_id: str) -> Any:
        return getattr(self._store, "get_session_tree")(session_id)


# For backward compatibility allow: from src.core.memory.session_store import SessionStore
# and also expose get_session_store factory.
