"""sqlite_store_collaborators.py — Private collaborator classes for SqliteSessionStore.

Extracted from the monolithic SqliteSessionStore to improve readability and
testability. Each collaborator handles one bounded concern:

- ``ConnectionManager``: DB path resolution and connection lifecycle.
- ``SchemaManager``:     DDL execution, migrations, FTS index setup.
- ``SnapshotManager``:  Snapshot persistence, retrieval, and revert logic.

These classes are *internal* to the sqlite_session_store package.  External
code should import ``SqliteSessionStore`` from ``sqlite_session_store.py``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manages SQLite connection lifecycle and DB path resolution.

    Parameters
    ----------
    workdir:
        Project root directory.
    agent_context_dir:
        Pre-discovered agent context directory (may be ``None``).
    """

    def __init__(
        self,
        workdir: Path,
        agent_context_dir: Optional[Path],
    ) -> None:
        self.workdir = workdir
        self._agent_context_dir: Optional[Path] = agent_context_dir
        self.db_path: Optional[Path] = None
        self._lock = threading.RLock()
        self._local = threading.local()
        self._writer_conn: Optional[sqlite3.Connection] = None
        self._thread_connections: Dict[int, sqlite3.Connection] = {}
        self._thread_connections_lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def resolve_db_path(self) -> Path:
        """Resolve and cache the on-disk SQLite DB path."""
        if self.db_path is not None:
            return self.db_path

        ac = self._agent_context_dir
        if ac is not None:
            ac_path = Path(ac)
        else:
            legacy_agent_context = self.workdir / ".agent-context"
            legacy_agent = self.workdir / ".agent"
            if legacy_agent_context.exists():
                ac_path = legacy_agent_context
            elif legacy_agent.exists():
                ac_path = legacy_agent
            else:
                try:
                    from src.tools.tools_config import agent_context_path
                    ac_path = agent_context_path(self.workdir)
                except Exception:
                    ac_path = self.workdir / ".codingAgent"

        self._agent_context_dir = ac_path
        self.db_path = ac_path / "session.db"
        return self.db_path

    # ------------------------------------------------------------------
    # Session locks
    # ------------------------------------------------------------------

    def get_session_lock(self, session_id: str) -> threading.Lock:
        key = session_id or "unknown"
        with self._session_locks_lock:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[key] = lock
            return lock

    # ------------------------------------------------------------------
    # Reader connection (per-thread)
    # ------------------------------------------------------------------

    def get_connection(self) -> sqlite3.Connection:
        dbp = self.resolve_db_path()
        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(str(dbp), timeout=30.0)
            self._local.connection = conn
            try:
                with self._thread_connections_lock:
                    self._thread_connections[threading.get_ident()] = conn
            except Exception:
                logger.debug(
                    "ConnectionManager: failed to register thread connection\n%s",
                    traceback.format_exc(),
                )
            self._local.connection.row_factory = sqlite3.Row
            self._local.connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection.execute("PRAGMA busy_timeout=1000")
        return self._local.connection

    # ------------------------------------------------------------------
    # Writer connection (shared, check_same_thread=False)
    # ------------------------------------------------------------------

    def get_writer_connection(self, on_first_create=None) -> sqlite3.Connection:
        """Return (or create) the shared writer connection.

        Parameters
        ----------
        on_first_create:
            Optional zero-argument callable invoked once after the connection
            is created.  Used by SqliteSessionStore to trigger ``_ensure_tables``.
        """
        if self._writer_conn is None:
            dbp = self.resolve_db_path()
            dbp.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(dbp), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=1000")
            self._writer_conn = conn
            if on_first_create is not None:
                try:
                    on_first_create()
                except Exception:
                    logger.debug(
                        "ConnectionManager: on_first_create callback failed\n%s",
                        traceback.format_exc(),
                    )
        return self._writer_conn

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            with self._thread_connections_lock:
                conns = list(self._thread_connections.items())
                self._thread_connections.clear()
        except Exception:
            conns = []
        for tid, conn in conns:
            try:
                conn.close()
            except Exception:
                logger.debug(
                    "ConnectionManager.close: failed to close thread connection %s\n%s",
                    tid,
                    traceback.format_exc(),
                )
        try:
            with self._lock:
                if self._writer_conn is not None:
                    try:
                        self._writer_conn.close()
                    except Exception:
                        logger.debug(
                            "ConnectionManager.close: failed to close writer connection\n%s",
                            traceback.format_exc(),
                        )
                    finally:
                        self._writer_conn = None
        except Exception:
            logger.debug(
                "ConnectionManager.close: unexpected error\n%s",
                traceback.format_exc(),
            )


# ---------------------------------------------------------------------------
# SchemaManager
# ---------------------------------------------------------------------------


class SchemaManager:
    """Handles DDL execution, FTS index setup, and schema migrations.

    Parameters
    ----------
    conn_manager:
        ``ConnectionManager`` to use for obtaining connections.
    schema_version:
        Target schema version integer.
    """

    def __init__(self, conn_manager: ConnectionManager, schema_version: int = 3) -> None:
        self._cm = conn_manager
        self.schema_version = schema_version

    def get_schema_version(self) -> int:
        return self.schema_version

    def ensure_tables(self) -> None:
        from src.core.memory.sqlite_store_schema import (
            fts_creation_script,
            fts_trigger_statements,
            schema_creation_script,
        )

        _ = self._cm.resolve_db_path()
        conn = self._cm.get_writer_connection()
        try:
            conn.executescript(schema_creation_script())
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(self.schema_version),),
            )
            conn.commit()
            self._ensure_fts_index(conn)
            self._run_migrations(conn)
        except Exception as e:
            logger.error("SchemaManager.ensure_tables failed: %s", e)

    def _ensure_fts_index(self, conn: sqlite3.Connection) -> None:
        from src.core.memory.sqlite_store_schema import (
            fts_creation_script,
            fts_trigger_statements,
        )

        try:
            conn.executescript(fts_creation_script())
            for statement in fts_trigger_statements():
                conn.execute(statement)
            conn.commit()
            logger.debug("SchemaManager: FTS5 index ready")
        except Exception as e:
            logger.warning("SchemaManager: FTS index creation failed: %s", e)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        from src.core.memory.sqlite_store_schema import schema_version_from_row

        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_version = schema_version_from_row(row)
        except Exception:
            current_version = 1

        if current_version >= self.schema_version:
            return

        logger.info(
            "SchemaManager: migrating from v%d to v%d",
            current_version,
            self.schema_version,
        )

        if current_version < 2 <= self.schema_version:
            self._migrate_v2(conn)
            current_version = 2

        if current_version < 3 <= self.schema_version:
            self._migrate_v3(conn)
            current_version = 3

        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(self.schema_version),),
        )
        conn.commit()
        logger.info("SchemaManager: migration complete to v%d", self.schema_version)

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_children (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_session_id TEXT NOT NULL,
                    child_session_id TEXT NOT NULL,
                    role TEXT,
                    task TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_children_parent ON session_children(parent_session_id);"
            )
            logger.debug("SchemaManager: v2 migration complete")
        except Exception as e:
            logger.warning("SchemaManager: v2 migration failed: %s", e)

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mistakes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    context TEXT,
                    tool TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS mistakes_fts USING fts5(
                    session_id,
                    summary,
                    context,
                    tool,
                    tokenize='porter unicode61'
                );
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS mistakes_ai AFTER INSERT ON mistakes BEGIN
                    INSERT INTO mistakes_fts (session_id, summary, context, tool)
                    VALUES (new.session_id, new.summary, new.context, new.tool);
                END;
                """
            )
            logger.debug("SchemaManager: v3 migration complete")
        except Exception as e:
            logger.warning("SchemaManager: v3 migration failed: %s", e)


# ---------------------------------------------------------------------------
# SnapshotManager
# ---------------------------------------------------------------------------


class SnapshotManager:
    """Handles snapshot persistence, retrieval, and session revert.

    Parameters
    ----------
    conn_manager:
        ``ConnectionManager`` to use for obtaining connections.
    store_lock:
        The store-wide ``threading.RLock`` used to serialise writes.
    """

    def __init__(
        self,
        conn_manager: ConnectionManager,
        store_lock: threading.RLock,
    ) -> None:
        self._cm = conn_manager
        self._lock = store_lock

    def save_snapshot(
        self,
        session_id: str,
        state_json: str,
        role: Optional[str] = None,
        task: Optional[str] = None,
    ) -> Optional[str]:
        from src.core.memory.sqlite_store_schema import serialise_snapshot_rows

        snap_id = uuid.uuid4().hex
        sid = session_id or "unknown"

        with self._lock:
            wconn = self._cm.get_writer_connection()
            try:
                try:
                    wconn.execute("BEGIN")
                except Exception:
                    pass

                wconn.execute(
                    "INSERT INTO session_snapshots (session_id, snapshot_id, state_json, role, task) VALUES (?, ?, ?, ?, ?)",
                    (sid, snap_id, state_json, role, task),
                )

                tables = (
                    "messages",
                    "tool_calls",
                    "errors",
                    "plans",
                    "decisions",
                    "session_children",
                )
                for tbl in tables:
                    try:
                        rows = wconn.execute(
                            f"SELECT * FROM {tbl} WHERE session_id=? ORDER BY created_at",
                            (sid,),
                        ).fetchall()
                        serialised = serialise_snapshot_rows(rows)
                        wconn.execute(
                            "INSERT INTO session_snapshot_rows (snapshot_id, table_name, rows_json) VALUES (?, ?, ?)",
                            (snap_id, tbl, json.dumps(serialised, ensure_ascii=False)),
                        )
                    except Exception:
                        pass

                wconn.commit()
            except Exception:
                try:
                    wconn.rollback()
                except Exception:
                    pass
                return None

        return snap_id

    def get_snapshot(self, session_id: str, snapshot_id: str) -> Optional[str]:
        conn = self._cm.get_connection()
        row = conn.execute(
            "SELECT state_json FROM session_snapshots WHERE session_id=? AND snapshot_id=?",
            (session_id or "unknown", snapshot_id),
        ).fetchone()
        if not row:
            return None
        return row["state_json"]

    def revert_session(
        self, session_id: str, keep_messages: Optional[object] = False
    ) -> Dict[str, Any]:
        """Revert session state.

        Backwards-compatible:
          - str  → treat as snapshot_id and restore to that snapshot.
          - bool → original keep_messages behaviour.
        """
        if isinstance(keep_messages, str):
            return self._revert_to_snapshot(session_id, keep_messages)
        return self._revert_session_keep_messages(session_id, bool(keep_messages))

    def _revert_to_snapshot(
        self, session_id: str, snap_id: str
    ) -> Dict[str, Any]:
        from src.core.memory.sqlite_store_session_ops import (
            delete_rows_after_snapshot,
            group_snapshot_rows,
            restore_snapshot_rows,
        )

        sid = session_id or "unknown"
        try:
            conn = self._cm.get_connection()
            row = conn.execute(
                "SELECT saved_at FROM session_snapshots WHERE session_id=? AND snapshot_id=?",
                (sid, snap_id),
            ).fetchone()
            if not row:
                return {"ok": False, "deleted": {}}
            saved_at = row[0]

            try:
                snapshot_rows = conn.execute(
                    "SELECT table_name, rows_json FROM session_snapshot_rows WHERE snapshot_id=?",
                    (snap_id,),
                ).fetchall()
            except Exception:
                snapshot_rows = []

            deleted: Dict[str, int] = {
                "messages": 0,
                "tool_calls": 0,
                "errors": 0,
                "plans": 0,
                "decisions": 0,
            }

            if snapshot_rows:
                with self._lock:
                    wconn = self._cm.get_writer_connection()
                    try:
                        try:
                            wconn.execute("BEGIN")
                        except Exception:
                            pass
                        deleted = restore_snapshot_rows(
                            conn=wconn,
                            session_id=sid,
                            grouped_rows=group_snapshot_rows(snapshot_rows),
                            initial_deleted=deleted,
                        )
                        try:
                            wconn.execute(
                                "DELETE FROM session_snapshots WHERE session_id=? AND datetime(saved_at) > datetime(?)",
                                (sid, saved_at),
                            )
                        except Exception:
                            pass
                        wconn.commit()
                    except Exception:
                        try:
                            wconn.rollback()
                        except Exception:
                            pass
                        raise
                return {"ok": True, "deleted": deleted}

            # Fallback: timestamp-based deletion
            with self._lock:
                wconn = self._cm.get_writer_connection()
                try:
                    try:
                        wconn.execute("BEGIN")
                    except Exception:
                        pass
                    deleted = delete_rows_after_snapshot(
                        conn=wconn,
                        session_id=sid,
                        saved_at=saved_at,
                    )
                    wconn.commit()
                except Exception:
                    try:
                        wconn.rollback()
                    except Exception:
                        pass
                    raise

            return {"ok": True, "deleted": deleted}
        except Exception:
            return {"ok": False, "deleted": {}}

    def _revert_session_keep_messages(
        self, session_id: str, keep_messages: bool = False
    ) -> Dict[str, Any]:
        from src.core.memory.sqlite_store_session_ops import keep_messages_delete_specs

        sid = session_id or "unknown"
        deleted: Dict[str, int] = {
            "messages": 0,
            "tool_calls": 0,
            "errors": 0,
            "plans": 0,
            "decisions": 0,
        }

        with self._lock:
            conn = self._cm.get_writer_connection()
            try:
                tables = keep_messages_delete_specs(keep_messages)
                for tbl, should_delete in tables:
                    if not should_delete:
                        continue
                    cur = conn.execute(
                        f"DELETE FROM {tbl} WHERE session_id=?", (sid,)
                    )
                    cnt = (
                        cur.rowcount
                        if cur is not None and cur.rowcount is not None
                        else 0
                    )
                    deleted[tbl] = cnt

                try:
                    conn.execute(
                        "DELETE FROM session_children WHERE parent_session_id=?", (sid,)
                    )
                except Exception:
                    pass
                try:
                    conn.execute(
                        "DELETE FROM session_snapshots WHERE session_id=?", (sid,)
                    )
                except Exception:
                    pass

                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error("SnapshotManager._revert_session_keep_messages failed: %s", e)
                raise

        return {"ok": True, "deleted": deleted}


__all__ = ["ConnectionManager", "SchemaManager", "SnapshotManager"]
