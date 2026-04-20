from __future__ import annotations
import os
import sqlite3
import time
import shutil
import json
import logging
import tempfile
import threading
import uuid

# ruff: noqa: E501
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SqliteSessionStore:
    """SQLite-backed session store extracted for long-term memory use.

    This implementation is retained for archival/long-term storage use cases
    but is not used by default for ephemeral session persistence.
    """

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.db_path = self.workdir / ".agent-context" / "session.db"
        self._lock = threading.RLock()
        self._local = threading.local()
        self._writer_conn: Optional[sqlite3.Connection] = None
        self._thread_connections = {}
        self._thread_connections_lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_lock = threading.Lock()
        self._ensure_tables()

    # For brevity this file contains the same logic previously present in
    # src/core/memory/session_store.py but the class is renamed to make the
    # separation explicit.  Consumers that require long-term SQLite-backed
    # memory should import SqliteSessionStore directly.

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        key = session_id or "unknown"
        with self._session_locks_lock:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[key] = lock
            return lock

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            self._local.connection = conn
            try:
                with self._thread_connections_lock:
                    self._thread_connections[threading.get_ident()] = conn
            except Exception:
                logger.debug(
                    "SqliteSessionStore: failed to register thread connection",
                    exc_info=True,
                )
            self._local.connection.row_factory = sqlite3.Row
            self._local.connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection.execute("PRAGMA busy_timeout=1000")
        return self._local.connection

    def _get_writer_connection(self) -> sqlite3.Connection:
        if self._writer_conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path), timeout=30.0, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=1000")
            self._writer_conn = conn
        return self._writer_conn

    _SCHEMA_VERSION = 2

    def get_schema_version(self) -> int:
        """Return the schema version for compatibility with other stores."""
        try:
            return int(self._SCHEMA_VERSION)
        except Exception:
            return 1

    def _ensure_tables(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_writer_connection()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args TEXT NOT NULL,
                    result TEXT,
                    success INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    error_type TEXT,
                    error_message TEXT,
                    context TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    rationale TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_children (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_session_id TEXT NOT NULL,
                    child_session_id TEXT NOT NULL,
                    role TEXT,
                    task TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
                CREATE INDEX IF NOT EXISTS idx_errors_session ON errors(session_id);
                CREATE INDEX IF NOT EXISTS idx_children_parent ON session_children(parent_session_id);
                CREATE TABLE IF NOT EXISTS session_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    role TEXT,
                    task TEXT,
                    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS session_snapshot_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(self._SCHEMA_VERSION),),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"SqliteSessionStore: failed to create tables: {e}")
        # Note: migrations and other helpers omitted for brevity.

    # ------------------------------------------------------------------
    # High-level convenience API (parity with JsonlSessionStore)
    # ------------------------------------------------------------------

    def _execute_write(self, sql: str, params: tuple = ()):
        try:
            with self._lock:
                conn = self._get_writer_connection()
                cur = conn.execute(sql, params)
                conn.commit()
                return cur
        except Exception as e:
            logger.error("SqliteSessionStore: write failed: %s", e)
            raise

    def add_message(self, session_id: str, role: str, content: str) -> None:
        sid = session_id or "unknown"
        self._execute_write(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (sid, role, content),
        )

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY created_at",
            (session_id or "unknown",),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def add_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: Any,
        result: Any,
        success: bool = True,
    ) -> None:
        sid = session_id or "unknown"
        try:
            args_text = json.dumps(args, ensure_ascii=False)
        except Exception:
            args_text = str(args)
        try:
            result_text = json.dumps(result, ensure_ascii=False)
        except Exception:
            result_text = str(result)
        self._execute_write(
            "INSERT INTO tool_calls (session_id, tool_name, args, result, success) VALUES (?, ?, ?, ?, ?)",
            (sid, tool_name, args_text, result_text, 1 if success else 0),
        )

    def get_tool_calls(self, session_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT tool_name, args, result, success FROM tool_calls WHERE session_id=? ORDER BY created_at",
            (session_id or "unknown",),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                args = json.loads(r["args"]) if r["args"] is not None else None
            except Exception:
                args = r["args"]
            try:
                res = json.loads(r["result"]) if r["result"] is not None else None
            except Exception:
                res = r["result"]
            out.append(
                {
                    "tool_name": r["tool_name"],
                    "args": args,
                    "result": res,
                    "success": bool(r["success"] or 0),
                }
            )
        return out

    def add_plan(self, session_id: str, plan: Any, status: str = "active") -> None:
        sid = session_id or "unknown"
        plan_text = (
            plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False)
        )
        self._execute_write(
            "INSERT INTO plans (session_id, plan, status) VALUES (?, ?, ?)",
            (sid, plan_text, status),
        )

    def get_plans(self, session_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT plan, status FROM plans WHERE session_id=? ORDER BY created_at",
            (session_id or "unknown",),
        ).fetchall()
        return [{"plan": r["plan"], "status": r["status"]} for r in rows]

    def add_error(
        self,
        session_id: str,
        error_type: str,
        error_message: str,
        context: Optional[Any] = None,
    ) -> None:
        sid = session_id or "unknown"
        try:
            ctx = (
                json.dumps(context, ensure_ascii=False) if context is not None else None
            )
        except Exception:
            ctx = str(context)
        self._execute_write(
            "INSERT INTO errors (session_id, error_type, error_message, context) VALUES (?, ?, ?, ?)",
            (sid, error_type, error_message, ctx),
        )

    def get_errors(self, session_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT error_type, error_message, context FROM errors WHERE session_id=? ORDER BY created_at",
            (session_id or "unknown",),
        ).fetchall()
        out = []
        for r in rows:
            ctx = r["context"]
            try:
                ctx = json.loads(ctx) if ctx is not None else None
            except Exception:
                pass
            out.append(
                {
                    "error_type": r["error_type"],
                    "error_message": r["error_message"],
                    "context": ctx,
                }
            )
        return out

    def add_decision(
        self, session_id: str, decision: str, rationale: Optional[str] = None
    ) -> None:
        sid = session_id or "unknown"
        self._execute_write(
            "INSERT INTO decisions (session_id, decision, rationale) VALUES (?, ?, ?)",
            (sid, decision, rationale),
        )
        # Auto-flush recent decisions to the decisions.json sidecar so callers
        # that expect an immediate on-disk decisions file observe the new
        # decision. This is best-effort: failures should not propagate.
        try:
            self.write_decisions_json()
        except Exception:
            logger.debug(
                "SqliteSessionStore: write_decisions_json failed", exc_info=True
            )

    def get_decisions(self, session_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT decision, rationale, created_at FROM decisions WHERE session_id=? ORDER BY created_at",
            (session_id or "unknown",),
        ).fetchall()
        return [
            {
                "decision": r["decision"],
                "rationale": r["rationale"],
                "ts": r["created_at"],
            }
            for r in rows
        ]

    def _write_with_retry(
        self,
        conn: Any,
        sql: str,
        params: tuple = (),
        session_id: Optional[str] = None,
        attempts: int = 5,
        base_backoff: float = 0.05,
    ) -> bool:
        """Retry a write against *conn* on SQLITE_BUSY/locked errors.

        Attempts to execute ``conn.execute(sql, params)`` and commit. On
        sqlite3.OperationalError containing 'lock' this retries with
        exponential backoff. On exhaustion a diagnostic JSON file is written
        into ``{workdir}/.agent-context/session_store_write_failure_*.json``
        and False is returned.
        """
        last_err: Optional[Exception] = None
        for i in range(1, max(1, attempts) + 1):
            try:
                if hasattr(conn, "execute"):
                    conn.execute(sql, params)
                if hasattr(conn, "commit"):
                    conn.commit()
                return True
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if isinstance(e, sqlite3.OperationalError) and "lock" in msg:
                    sleep_for = base_backoff * (2 ** (i - 1))
                    try:
                        time.sleep(sleep_for)
                    except Exception:
                        pass
                    continue
                else:
                    break

        # Exhausted or unrecoverable error — write diagnostic
        try:
            diag_dir = Path(self.workdir) / ".agent-context"
            diag_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            sid = session_id or "unknown"
            diag_path = diag_dir / f"session_store_write_failure_{ts}_{sid}.json"
            payload = {
                "db_path": str(self.db_path) if hasattr(self, "db_path") else None,
                "session_id": sid,
                "attempts": attempts,
                "last_error": (
                    "SQLITE_BUSY/LOCKED"
                    if isinstance(last_err, sqlite3.OperationalError)
                    and "lock" in str(last_err).lower()
                    else str(last_err)
                ),
                "sql": sql,
                "params": params,
                "ts": int(time.time()),
            }

            fd = None
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=str(diag_dir), suffix=".tmp")
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
            logger.debug(
                "SqliteSessionStore._write_with_retry: failed to write diagnostic",
                exc_info=True,
            )

        return False

    def write_decisions_json(self, limit: int = 50) -> None:
        """Collect recent decisions from the DB and write decisions.json sidecar.

        The most recent decisions are selected by created_at descending.
        """
        try:
            conn = self._get_connection()
            cur = conn.execute(
                "SELECT session_id, decision, rationale, created_at FROM decisions ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            )
            rows = cur.fetchall()
            decisions: List[Dict[str, Any]] = []
            for r in rows:
                decisions.append(
                    {
                        "session_id": r["session_id"],
                        "decision": r["decision"],
                        "rationale": r["rationale"],
                        "ts": r["created_at"],
                    }
                )

            out_dir = Path(self.workdir) / ".agent-context"
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
        except Exception:
            # Best-effort: do not propagate failures
            logger.debug(
                "SqliteSessionStore.write_decisions_json failed", exc_info=True
            )

    def read_recent_decisions(self, max_entries: int = 10) -> List[Dict[str, Any]]:
        """Read recent decisions from DB (preferred) or from decisions.json sidecar.

        Returns an empty list on error or when no decisions exist.
        """
        try:
            conn = self._get_connection()
            cur = conn.execute(
                "SELECT session_id, decision, rationale, created_at FROM decisions ORDER BY created_at DESC LIMIT ?",
                (int(max_entries),),
            )
            rows = cur.fetchall()
            decisions: List[Dict[str, Any]] = []
            for r in rows:
                decisions.append(
                    {
                        "session_id": r["session_id"],
                        "decision": r["decision"],
                        "rationale": r["rationale"],
                        "ts": r["created_at"],
                    }
                )
            return decisions
        except Exception:
            # Fallback to sidecar file
            try:
                path = Path(self.workdir) / ".agent-context" / "decisions.json"
                if not path.exists():
                    return []
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    return []
                return data[: max(0, int(max_entries))]
            except Exception:
                return []

    def register_child_session(
        self,
        parent_session_id: str,
        child_session_id: str,
        role: Optional[str] = None,
        task: Optional[str] = None,
    ) -> None:
        self._execute_write(
            "INSERT INTO session_children (parent_session_id, child_session_id, role, task) VALUES (?, ?, ?, ?)",
            (parent_session_id or "unknown", child_session_id, role, task),
        )

    def get_child_sessions(self, parent_session_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT parent_session_id, child_session_id, role, task FROM session_children WHERE parent_session_id=? ORDER BY created_at",
            (parent_session_id or "unknown",),
        ).fetchall()
        return [
            {
                "parent_session_id": r["parent_session_id"],
                "child_session_id": r["child_session_id"],
                "role": r["role"],
                "task": r["task"],
            }
            for r in rows
        ]

    def get_session_tree(self, session_id: str) -> Dict[str, Any]:
        # Build parent->children map by scanning session_children
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT parent_session_id, child_session_id FROM session_children"
        ).fetchall()
        map_parent: Dict[str, List[str]] = {}
        for r in rows:
            p = r["parent_session_id"]
            c = r["child_session_id"]
            map_parent.setdefault(str(p), []).append(str(c))

        def _build(sid: str) -> Dict[str, Any]:
            children = []
            for ch in map_parent.get(str(sid), []):
                children.append(_build(ch))
            return {"session_id": sid, "children": children}

        return _build(session_id)

    def save_snapshot(
        self,
        session_id: str,
        state_json: str,
        role: Optional[str] = None,
        task: Optional[str] = None,
    ) -> str:
        """Create a durable snapshot capturing both a sidecar state and a
        row-level copy of session rows across the mutable tables.

        The row-level copy is stored in session_snapshot_rows and is used by
        revert_session to restore the session exactly as it was when the
        snapshot was taken. This provides a stronger and more reliable revert
        semantics than timestamp-only truncation.
        """
        snap_id = uuid.uuid4().hex
        sid = session_id or "unknown"

        with self._lock:
            wconn = self._get_writer_connection()
            try:
                try:
                    wconn.execute("BEGIN")
                except Exception:
                    pass

                # Insert snapshot metadata
                wconn.execute(
                    "INSERT INTO session_snapshots (session_id, snapshot_id, state_json, role, task) VALUES (?, ?, ?, ?, ?)",
                    (sid, snap_id, state_json, role, task),
                )

                # Tables to snapshot (preserve ordering via created_at)
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
                        # Convert sqlite3.Row to serialisable dicts excluding the
                        # primary key (id) and the session_id column so the
                        # snapshot can be restored into the same session_id.
                        serialised: List[Dict[str, Any]] = []
                        for r in rows:
                            d = dict(r)
                            # Remove primary key and session_id columns if present
                            d.pop("id", None)
                            d.pop("session_id", None)
                            serialised.append(d)
                        wconn.execute(
                            "INSERT INTO session_snapshot_rows (snapshot_id, table_name, rows_json) VALUES (?, ?, ?)",
                            (snap_id, tbl, json.dumps(serialised, ensure_ascii=False)),
                        )
                    except Exception:
                        # Best-effort: skip tables that may not exist
                        pass

                wconn.commit()
            except Exception:
                try:
                    wconn.rollback()
                except Exception:
                    pass
                raise

        return snap_id

    def get_snapshot(self, session_id: str, snapshot_id: str) -> Optional[str]:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT state_json FROM session_snapshots WHERE session_id=? AND snapshot_id=?",
            (session_id or "unknown", snapshot_id),
        ).fetchone()
        if not row:
            return None
        return row["state_json"]

    def list_sessions(self) -> List[str]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM (SELECT session_id FROM messages UNION SELECT session_id FROM tool_calls UNION SELECT session_id FROM errors UNION SELECT session_id FROM plans UNION SELECT session_id FROM decisions)"
        ).fetchall()
        return sorted({r[0] for r in rows if r[0] is not None})

    def delete_session(self, session_id: str) -> int:
        # Delete rows in all tables for the session; return total deleted rows
        total = 0
        with self._lock:
            conn = self._get_writer_connection()
            for tbl in (
                "messages",
                "tool_calls",
                "errors",
                "plans",
                "decisions",
                "session_children",
                "session_snapshots",
            ):
                cur = conn.execute(
                    f"DELETE FROM {tbl} WHERE session_id=?", (session_id or "unknown",)
                )
                total += (
                    cur.rowcount if cur is not None and cur.rowcount is not None else 0
                )
            conn.commit()
        return total

    def fork_session(self, session_id: str, fork_id: Optional[str] = None) -> str:
        """Create an independent copy of *session_id* with a new session id.

        Copies rows from the main tables into new rows using *fork_id* when
        provided or a generated UUID4 hex string. Raises ValueError when the
        source session does not exist.
        """
        sid = session_id or "unknown"
        # Determine whether the session exists by scanning known tables
        if sid not in self.list_sessions():
            raise ValueError(
                f"fork_session: source session '{session_id}' does not exist"
            )

        new_id = fork_id or uuid.uuid4().hex

        with self._lock:
            conn = self._get_writer_connection()
            try:
                # Copy messages (preserve created_at ordering)
                rows = conn.execute(
                    "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY created_at",
                    (sid,),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                        (new_id, r["role"], r["content"], r["created_at"]),
                    )

                # Copy tool_calls
                rows = conn.execute(
                    "SELECT tool_name, args, result, success, created_at FROM tool_calls WHERE session_id=? ORDER BY created_at",
                    (sid,),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "INSERT INTO tool_calls (session_id, tool_name, args, result, success, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            new_id,
                            r["tool_name"],
                            r["args"],
                            r["result"],
                            r["success"],
                            r["created_at"],
                        ),
                    )

                # Copy errors
                rows = conn.execute(
                    "SELECT error_type, error_message, context, created_at FROM errors WHERE session_id=? ORDER BY created_at",
                    (sid,),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "INSERT INTO errors (session_id, error_type, error_message, context, created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            new_id,
                            r["error_type"],
                            r["error_message"],
                            r["context"],
                            r["created_at"],
                        ),
                    )

                # Copy plans
                rows = conn.execute(
                    "SELECT plan, status, created_at FROM plans WHERE session_id=? ORDER BY created_at",
                    (sid,),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "INSERT INTO plans (session_id, plan, status, created_at) VALUES (?, ?, ?, ?)",
                        (new_id, r["plan"], r["status"], r["created_at"]),
                    )

                # Copy decisions
                rows = conn.execute(
                    "SELECT decision, rationale, created_at FROM decisions WHERE session_id=? ORDER BY created_at",
                    (sid,),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "INSERT INTO decisions (session_id, decision, rationale, created_at) VALUES (?, ?, ?, ?)",
                        (new_id, r["decision"], r["rationale"], r["created_at"]),
                    )

                # Copy session_children where this session is the parent
                rows = conn.execute(
                    "SELECT child_session_id, role, task, created_at FROM session_children WHERE parent_session_id=? ORDER BY created_at",
                    (sid,),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "INSERT INTO session_children (parent_session_id, child_session_id, role, task, created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            new_id,
                            r["child_session_id"],
                            r["role"],
                            r["task"],
                            r["created_at"],
                        ),
                    )

                # Copy snapshots (preserve snapshot_id) and ensure the
                # associated row-level snapshot data is available for the
                # forked session. session_snapshot_rows is keyed by
                # snapshot_id so preserving the same snapshot_id means the
                # fork can use the same row-level data. To be defensive we
                # also insert any missing session_snapshot_rows rows (avoids
                # surprising gaps if the rows were stored separately).
                rows = conn.execute(
                    "SELECT snapshot_id, state_json, role, task, saved_at FROM session_snapshots WHERE session_id=? ORDER BY saved_at",
                    (sid,),
                ).fetchall()

                snapshot_ids = []
                for r in rows:
                    snapshot_ids.append(r["snapshot_id"])
                    conn.execute(
                        "INSERT INTO session_snapshots (session_id, snapshot_id, state_json, role, task, saved_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            new_id,
                            r["snapshot_id"],
                            r["state_json"],
                            r["role"],
                            r["task"],
                            r["saved_at"],
                        ),
                    )

                # Copy any missing session_snapshot_rows for the snapshot ids we
                # just attached to the fork. We check for an existing identical
                # (snapshot_id, table_name, rows_json) row before inserting to
                # avoid creating duplicates.
                try:
                    for snap in snapshot_ids:
                        srows = conn.execute(
                            "SELECT table_name, rows_json, saved_at FROM session_snapshot_rows WHERE snapshot_id=?",
                            (snap,),
                        ).fetchall()
                        for sr in srows:
                            try:
                                exists = conn.execute(
                                    "SELECT 1 FROM session_snapshot_rows WHERE snapshot_id=? AND table_name=? AND rows_json=? LIMIT 1",
                                    (snap, sr["table_name"], sr["rows_json"]),
                                ).fetchone()
                                if exists:
                                    continue
                                conn.execute(
                                    "INSERT INTO session_snapshot_rows (snapshot_id, table_name, rows_json, saved_at) VALUES (?, ?, ?, ?)",
                                    (
                                        snap,
                                        sr["table_name"],
                                        sr["rows_json"],
                                        sr["saved_at"],
                                    ),
                                )
                            except Exception:
                                # Best-effort: skip problematic snapshot rows
                                continue
                except Exception:
                    # Best-effort: if the snapshot rows query fails, ignore
                    pass

                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error("SqliteSessionStore.fork_session failed: %s", e)
                raise

        return new_id

    # Backwards-compatibility shim: some callers expect the signature
    # fork_session(session_id, new_session_id). Provide an alias that maps
    # the older positional name to the current implementation.
    # Note: fork_session_alias removed — callers should use fork_session(..., fork_id=...)

    # Original revert implementation removed in favour of a compatibility
    # wrapper below that accepts both the legacy boolean ``keep_messages``
    # parameter and a snapshot_id string. The wrapper delegates to the
    # private helper _revert_session_keep_messages when a boolean is used.

    # Compatibility overload: accept a snapshot_id as the second argument so
    # callers using the JsonlSessionStore semantics (revert_session(session_id, snapshot_id))
    # can still call revert_session on the sqlite-backed store. If the
    # second parameter is a string we interpret it as a snapshot_id and restore
    # the session state to that snapshot by deleting rows newer than the
    # snapshot's saved_at timestamp. This is best-effort and may be a no-op
    # if the snapshot is not found.
    def revert_session(
        self, session_id: str, keep_messages: Optional[object] = False
    ) -> Dict[str, Any]:
        """Revert session state.

        Backwards-compatible behaviour:
          - If ``keep_messages`` is a bool: original behaviour where True
            preserves messages and deletes other tables.
          - If ``keep_messages`` is a str: treat it as a ``snapshot_id`` and
            attempt to revert to that snapshot timestamp (best-effort).
        """
        # If a string was provided, treat as snapshot_id and attempt to revert
        if isinstance(keep_messages, str):
            snap_id = keep_messages
            sid = session_id or "unknown"
            try:
                conn = self._get_connection()
                row = conn.execute(
                    "SELECT saved_at FROM session_snapshots WHERE session_id=? AND snapshot_id=?",
                    (sid, snap_id),
                ).fetchone()
                if not row:
                    return {"ok": False, "deleted": {}}
                saved_at = row[0]

                # Prefer a deterministic, row-level restore when snapshot row
                # data is available. This restores the exact set of rows that
                # were present when save_snapshot() ran. If no row-level data
                # exists for this snapshot, fall back to the timestamp-based
                # deletion approach as a best-effort revert.
                try:
                    conn = self._get_connection()
                    rows = conn.execute(
                        "SELECT table_name, rows_json FROM session_snapshot_rows WHERE snapshot_id=?",
                        (snap_id,),
                    ).fetchall()
                except Exception:
                    rows = []

                if rows:
                    deleted: Dict[str, int] = {
                        "messages": 0,
                        "tool_calls": 0,
                        "errors": 0,
                        "plans": 0,
                        "decisions": 0,
                    }
                    with self._lock:
                        wconn = self._get_writer_connection()
                        try:
                            try:
                                wconn.execute("BEGIN")
                            except Exception:
                                pass

                            # Group rows by table_name
                            by_table: Dict[str, List[str]] = {}
                            for r in rows:
                                tbl = r[0]
                                js = r[1]
                                by_table.setdefault(tbl, []).append(js)

                            for tbl, js_list in by_table.items():
                                try:
                                    # Delete existing session rows for this table
                                    if tbl == "session_children":
                                        cur = wconn.execute(
                                            "DELETE FROM session_children WHERE parent_session_id=?",
                                            (sid,),
                                        )
                                    else:
                                        cur = wconn.execute(
                                            f"DELETE FROM {tbl} WHERE session_id=?",
                                            (sid,),
                                        )
                                    # Attempt to capture rowcount where supported
                                    if cur is not None:
                                        try:
                                            deleted[tbl] = cur.rowcount  # type: ignore[index]
                                        except Exception:
                                            pass
                                except Exception:
                                    pass

                                # Insert the snapshot rows back
                                for js in js_list:
                                    try:
                                        items = json.loads(js)
                                        for item in items:
                                            # Ensure the session-identifying column
                                            # is present and set to the target session.
                                            if tbl == "session_children":
                                                item["parent_session_id"] = sid
                                            else:
                                                item["session_id"] = sid

                                            cols = list(item.keys())
                                            vals = [item[c] for c in cols]
                                            placeholders = ",".join(["?" for _ in cols])
                                            col_list = ",".join(cols)
                                            try:
                                                wconn.execute(
                                                    f"INSERT INTO {tbl} ({col_list}) VALUES ({placeholders})",
                                                    tuple(vals),
                                                )
                                            except Exception:
                                                # If a column mismatch occurs or insert fails,
                                                # skip the row (best-effort restore)
                                                pass
                                    except Exception:
                                        pass

                            # Optionally remove snapshots created after this snap
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

                # No deterministic row-level snapshot data — fall back to
                # timestamp-based deletion as a last resort.
                deleted = {
                    "messages": 0,
                    "tool_calls": 0,
                    "errors": 0,
                    "plans": 0,
                    "decisions": 0,
                }
                with self._lock:
                    wconn = self._get_writer_connection()
                    try:
                        try:
                            wconn.execute("BEGIN")
                        except Exception:
                            pass
                        tables = (
                            "tool_calls",
                            "errors",
                            "plans",
                            "decisions",
                        )
                        for tbl in tables:
                            try:
                                cur = wconn.execute(
                                    f"DELETE FROM {tbl} WHERE session_id=? AND datetime(created_at) > datetime(?)",
                                    (sid, saved_at),
                                )
                                deleted[tbl] = (
                                    cur.rowcount
                                    if cur is not None and cur.rowcount is not None
                                    else 0
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
            except Exception:
                return {"ok": False, "deleted": {}}

        # Otherwise fall back to original behaviour (keep_messages boolean)
        return self._revert_session_keep_messages(session_id, bool(keep_messages))

    # Preserve the original implementation under a private name to be invoked
    # by the compatibility wrapper above when a boolean keep_messages is used.
    def _revert_session_keep_messages(
        self, session_id: str, keep_messages: bool = False
    ) -> Dict[str, Any]:
        sid = session_id or "unknown"
        deleted: Dict[str, int] = {
            "messages": 0,
            "tool_calls": 0,
            "errors": 0,
            "plans": 0,
            "decisions": 0,
        }

        with self._lock:
            conn = self._get_writer_connection()
            try:
                tables = [
                    ("messages", not keep_messages),
                    ("tool_calls", True),
                    ("errors", True),
                    ("plans", True),
                    ("decisions", True),
                ]
                for tbl, should_delete in tables:
                    if not should_delete:
                        continue
                    cur = conn.execute(f"DELETE FROM {tbl} WHERE session_id=?", (sid,))
                    cnt = (
                        cur.rowcount
                        if cur is not None and cur.rowcount is not None
                        else 0
                    )
                    # Map to the canonical key used by tests
                    key = tbl
                    deleted[key] = cnt

                # Also remove session_children and snapshots for the session
                try:
                    cur = conn.execute(
                        "DELETE FROM session_children WHERE parent_session_id=?", (sid,)
                    )
                except Exception:
                    cur = None
                try:
                    conn.execute(
                        "DELETE FROM session_snapshots WHERE session_id=?", (sid,)
                    )
                except Exception:
                    pass

                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error("SqliteSessionStore.revert_session failed: %s", e)
                raise

        return {"ok": True, "deleted": deleted}

    def session_exists(self, session_id: str) -> bool:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT 1 FROM messages WHERE session_id=? LIMIT 1",
            (session_id or "unknown",),
        ).fetchone()
        return bool(row)

    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        sid = session_id or "unknown"
        conn = self._get_connection()
        summary = {
            "session_id": sid,
            "messages": 0,
            "message_count": 0,
            "tool_calls": 0,
            "tool_call_count": 0,
            "errors": 0,
            "error_count": 0,
            "plans": 0,
            "decisions": 0,
        }
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)
            ).fetchone()
            summary["messages"] = int(row[0]) if row else 0
            summary["message_count"] = int(row[0]) if row else 0
        except Exception:
            pass
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE session_id=?", (sid,)
            ).fetchone()
            c = int(row[0]) if row else 0
            summary["tool_calls"] = c
            summary["tool_call_count"] = c
        except Exception:
            pass
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM errors WHERE session_id=?", (sid,)
            ).fetchone()
            c = int(row[0]) if row else 0
            summary["errors"] = c
            summary["error_count"] = c
        except Exception:
            pass
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM plans WHERE session_id=?", (sid,)
            ).fetchone()
            summary["plans"] = int(row[0]) if row else 0
        except Exception:
            pass
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE session_id=?", (sid,)
            ).fetchone()
            summary["decisions"] = int(row[0]) if row else 0
        except Exception:
            pass
        return summary

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
                    "SqliteSessionStore.close: failed to close thread connection %s",
                    tid,
                    exc_info=True,
                )
        try:
            with self._lock:
                if self._writer_conn is not None:
                    try:
                        self._writer_conn.close()
                    except Exception:
                        logger.debug(
                            "SqliteSessionStore.close: failed to close writer connection",
                            exc_info=True,
                        )
                    finally:
                        self._writer_conn = None
        except Exception:
            logger.debug(
                "SqliteSessionStore.close: unexpected error during close", exc_info=True
            )
