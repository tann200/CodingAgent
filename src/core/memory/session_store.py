from __future__ import annotations
import sqlite3
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionStore:
    """SQLite-based session store for conversation retrieval and debugging."""

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.db_path = self.workdir / ".agent-context" / "session.db"
        self._lock = threading.RLock()
        self._local = threading.local()  # instance-level, not shared across instances
        self._ensure_tables()

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                # check_same_thread omitted (default True): threading.local() already
                # guarantees each thread creates and owns its own connection, so
                # allowing cross-thread use would be a contradictory no-op (SCAN2-5).
            )
            self._local.connection.row_factory = sqlite3.Row
            # Enable WAL mode and busy timeout for concurrent access
            self._local.connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection.execute("PRAGMA busy_timeout=5000")
        return self._local.connection

    # SES-W1: current SQLite schema version.  Increment when making breaking
    # schema changes so old databases can be detected and migrated.
    _SCHEMA_VERSION = 1

    def _ensure_tables(self):
        """Create tables if they don't exist, reusing the thread-local connection."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Reuse the thread-local connection instead of creating a separate one (H9 fix).
        conn = self._get_connection()
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
            """)
            # Write schema version once; ignore if already set.
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(self._SCHEMA_VERSION),),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"SessionStore: failed to create tables: {e}")

    def get_schema_version(self) -> int:
        """Return the stored schema version, or 0 for pre-versioned databases."""
        with self._lock:
            try:
                conn = self._get_connection()
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                return int(row[0]) if row else 0
            except Exception:
                return 0

    def add_message(self, session_id: str, role: str, content: str):
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute(
                    "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                    (session_id, role, content),
                )
                conn.commit()
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to add message for session {session_id}: {e}"
                )

    def get_messages(self, session_id: str, limit: int = 100) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT id, role, content, created_at FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
                    (session_id, limit),
                )
                return [
                    {
                        "id": row[0],
                        "role": row[1],
                        "content": row[2],
                        "created_at": row[3],
                    }
                    for row in cursor.fetchall()
                ]
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to get messages for session {session_id}: {e}"
                )
                return []

    def add_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: Dict,
        result: Any = None,
        success: bool = True,
    ):
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute(
                    "INSERT INTO tool_calls (session_id, tool_name, args, result, success) VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        tool_name,
                        json.dumps(args),
                        json.dumps(result) if result else None,
                        1 if success else 0,
                    ),
                )
                conn.commit()
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to add tool_call for session {session_id}: {e}"
                )

    def get_tool_calls(self, session_id: str, limit: int = 100) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT id, tool_name, args, result, success, created_at FROM tool_calls WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                )
                return [
                    {
                        "id": row[0],
                        "tool_name": row[1],
                        "args": json.loads(row[2]) if row[2] else {},
                        "result": json.loads(row[3]) if row[3] else None,
                        "success": bool(row[4]),
                        "created_at": row[5],
                    }
                    for row in cursor.fetchall()
                ]
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to get tool_calls for session {session_id}: {e}"
                )
                return []

    def add_error(
        self,
        session_id: str,
        error_type: str,
        error_message: str,
        context: Optional[Dict] = None,
    ):
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute(
                    "INSERT INTO errors (session_id, error_type, error_message, context) VALUES (?, ?, ?, ?)",
                    (
                        session_id,
                        error_type,
                        error_message,
                        json.dumps(context) if context else None,
                    ),
                )
                conn.commit()
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to add error for session {session_id}: {e}"
                )

    def get_errors(self, session_id: str, limit: int = 50) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT id, error_type, error_message, context, created_at FROM errors WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                )
                return [
                    {
                        "id": row[0],
                        "error_type": row[1],
                        "error_message": row[2],
                        "context": json.loads(row[3]) if row[3] else None,
                        "created_at": row[4],
                    }
                    for row in cursor.fetchall()
                ]
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to get errors for session {session_id}: {e}"
                )
                return []

    def add_plan(self, session_id: str, plan: str, status: str = "active"):
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute(
                    "INSERT INTO plans (session_id, plan, status) VALUES (?, ?, ?)",
                    (session_id, plan, status),
                )
                conn.commit()
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to add plan for session {session_id}: {e}"
                )

    def get_plans(self, session_id: str) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT id, plan, status, created_at FROM plans WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,),
                )
                return [
                    {
                        "id": row[0],
                        "plan": row[1],
                        "status": row[2],
                        "created_at": row[3],
                    }
                    for row in cursor.fetchall()
                ]
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to get plans for session {session_id}: {e}"
                )
                return []

    def add_decision(
        self, session_id: str, decision: str, rationale: Optional[str] = None
    ):
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute(
                    "INSERT INTO decisions (session_id, decision, rationale) VALUES (?, ?, ?)",
                    (session_id, decision, rationale),
                )
                conn.commit()
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to add decision for session {session_id}: {e}"
                )
        # MEM-2: Flush decisions.json after each write (non-critical, best-effort).
        self.write_decisions_json()

    def get_decisions(self, session_id: str) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT id, decision, rationale, created_at FROM decisions WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,),
                )
                return [
                    {
                        "id": row[0],
                        "decision": row[1],
                        "rationale": row[2],
                        "created_at": row[3],
                    }
                    for row in cursor.fetchall()
                ]
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to get decisions for session {session_id}: {e}"
                )
                return []

    # MEM-2: Cross-session persistent decision memory
    # -----------------------------------------------------------------------

    def write_decisions_json(self, limit: int = 50) -> None:
        """MEM-2: Export the most recent *limit* decisions (all sessions) to
        ``{workdir}/.agent-context/decisions.json`` for cross-session recall.

        Called after every ``add_decision()`` so the file stays current.
        Failures are logged but never propagated — this is a best-effort export.
        """
        import json as _json
        import os as _os
        import tempfile as _tempfile

        try:
            # Acquire lock only for the SQLite read; release before filesystem I/O
            # to avoid blocking other threads (e.g. add_message, add_plan) during
            # potentially slow disk writes.
            with self._lock:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT session_id, decision, rationale, created_at "
                    "FROM decisions ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = [
                    {
                        "session_id": r[0],
                        "decision": r[1],
                        "rationale": r[2],
                        "created_at": r[3],
                    }
                    for r in cursor.fetchall()
                ]
            # Lock released — now do filesystem I/O outside the lock
            out_path = self.workdir / ".agent-context" / "decisions.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = _tempfile.mkstemp(dir=str(out_path.parent), suffix=".tmp")
            _fd_open = False
            try:
                with _os.fdopen(fd, "w", encoding="utf-8") as f:
                    _fd_open = True  # fdopen took ownership; fd is now managed by f
                    _json.dump(rows, f, ensure_ascii=False, indent=2)
                _os.replace(tmp, str(out_path))
            except Exception:
                if not _fd_open:
                    # fdopen itself failed — fd was never wrapped, close it manually
                    try:
                        _os.close(fd)
                    except OSError:
                        pass
                try:
                    _os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.debug(
                f"SessionStore.write_decisions_json: failed (non-critical): {e}"
            )

    def read_recent_decisions(self, max_entries: int = 10) -> List[Dict]:
        """MEM-2: Read recent decisions from ``decisions.json`` on disk.

        Falls back to an empty list when the file is absent or unreadable.
        Designed to be called by ``perception_node`` before building the system
        prompt so the LLM is aware of historical task outcomes.
        """
        import json as _json

        try:
            decisions_path = self.workdir / ".agent-context" / "decisions.json"
            if not decisions_path.exists():
                return []
            data = _json.loads(decisions_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            # Filter to only valid dict entries to avoid AttributeError on .get()
            return [d for d in data if isinstance(d, dict)][:max_entries]
        except Exception as e:
            logger.debug(
                f"SessionStore.read_recent_decisions: failed (non-critical): {e}"
            )
            return []

    # SPAWN-W3 / SPAWN-W4: child session registration and hierarchy queries
    # -----------------------------------------------------------------------

    def register_child_session(
        self,
        parent_session_id: str,
        child_session_id: str,
        role: str = "",
        task: str = "",
    ) -> None:
        """Record a parent→child delegation link in session_children.

        Called by delegate_task() after the child session completes so the
        hierarchy is queryable via get_child_sessions().
        """
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute(
                    "INSERT OR IGNORE INTO session_children "
                    "(parent_session_id, child_session_id, role, task) VALUES (?, ?, ?, ?)",
                    (
                        parent_session_id,
                        child_session_id,
                        role,
                        task[:500] if task else "",
                    ),
                )
                conn.commit()
            except Exception as e:
                logger.error(
                    "SessionStore: failed to register child session %s → %s: %s",
                    parent_session_id,
                    child_session_id,
                    e,
                )

    def get_child_sessions(self, parent_session_id: str) -> List[Dict]:
        """Return all direct children of *parent_session_id*.

        Each entry is a dict with keys: child_session_id, role, task, created_at.
        """
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT child_session_id, role, task, created_at "
                    "FROM session_children WHERE parent_session_id = ? "
                    "ORDER BY created_at",
                    (parent_session_id,),
                )
                return [
                    {
                        "child_session_id": row[0],
                        "role": row[1],
                        "task": row[2],
                        "created_at": row[3],
                    }
                    for row in cursor.fetchall()
                ]
            except Exception as e:
                logger.error(
                    "SessionStore: failed to get children of %s: %s",
                    parent_session_id,
                    e,
                )
                return []

    def get_session_tree(self, root_session_id: str) -> Dict:
        """Return a tree dict representing the full delegation hierarchy.

        Format: {"session_id": <id>, "children": [<tree>, ...], "role": <role>, "task": <task>}
        """
        children = self.get_child_sessions(root_session_id)
        return {
            "session_id": root_session_id,
            "role": "",
            "task": "",
            "children": [
                self.get_session_tree(c["child_session_id"]) for c in children
            ],
        }

    def list_sessions(self) -> List[str]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT session_id FROM messages GROUP BY session_id ORDER BY MAX(created_at) DESC"
                )
                return [row[0] for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"SessionStore: failed to list sessions: {e}")
                return []

    def get_session_summary(self, session_id: str) -> Dict:
        with self._lock:
            try:
                conn = self._get_connection()
                msg_count = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
                tool_count = conn.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                error_count = conn.execute(
                    "SELECT COUNT(*) FROM errors WHERE session_id = ?", (session_id,)
                ).fetchone()[0]

                return {
                    "session_id": session_id,
                    "message_count": msg_count,
                    "tool_call_count": tool_count,
                    "error_count": error_count,
                }
            except Exception as e:
                logger.error(
                    f"SessionStore: failed to get session summary for {session_id}: {e}"
                )
                return {
                    "session_id": session_id,
                    "message_count": 0,
                    "tool_call_count": 0,
                    "error_count": 0,
                }

    # ------------------------------------------------------------------
    # S5-A: Session fork
    # ------------------------------------------------------------------

    def fork_session(self, source_id: str, fork_id: Optional[str] = None) -> str:
        """Copy all rows for *source_id* into a new session with *fork_id*.

        Returns the new session id.  Raises ``ValueError`` if *source_id* does
        not exist.  If *fork_id* is not supplied a UUID4-based id is generated.

        The copy is shallow (rows only, no blobs).  The forked session starts
        with the full history of the source at the moment of forking — future
        writes to either session are independent.
        """
        import uuid as _uuid

        if fork_id is None:
            fork_id = str(_uuid.uuid4())

        _TABLES = [
            # (table, columns_without_id_or_session)
            (
                "messages",
                "role, content, created_at",
            ),
            (
                "tool_calls",
                "tool_name, args, result, success, created_at",
            ),
            (
                "errors",
                "error_type, error_message, context, created_at",
            ),
            (
                "plans",
                "plan, status, created_at",
            ),
            (
                "decisions",
                "decision, rationale, created_at",
            ),
        ]

        with self._lock:
            conn = self._get_connection()
            # Verify source exists
            exists = conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                (source_id,),
            ).fetchone()
            if not exists:
                # Also check other tables
                exists = conn.execute(
                    "SELECT 1 FROM tool_calls WHERE session_id = ? LIMIT 1",
                    (source_id,),
                ).fetchone()
            if not exists:
                raise ValueError(
                    f"fork_session: source session '{source_id}' does not exist"
                )

            try:
                for table, cols in _TABLES:
                    conn.execute(
                        f"INSERT INTO {table} (session_id, {cols}) "
                        f"SELECT ?, {cols} FROM {table} WHERE session_id = ?",
                        (fork_id, source_id),
                    )
                conn.commit()
                logger.info(f"SessionStore: forked session '{source_id}' → '{fork_id}'")
            except Exception as e:
                conn.rollback()
                logger.error(f"SessionStore: fork_session failed: {e}")
                raise

        return fork_id

    # ------------------------------------------------------------------
    # S5-B: Session revert
    # ------------------------------------------------------------------

    def revert_session(
        self,
        session_id: str,
        keep_messages: bool = False,
    ) -> Dict[str, Any]:
        """Remove all mutable data for *session_id* from the store.

        By default ALL rows (messages, tool_calls, errors, plans, decisions)
        are deleted.  Pass ``keep_messages=True`` to preserve the conversation
        history while clearing tool / error / plan artefacts.

        Returns a summary dict with ``{"ok": True, "deleted": {...}}`` where
        the nested dict maps table name → rows deleted.

        This is the *database* half of session revert.  The *file-system* half
        (restoring working-directory files to a prior git snapshot) is handled
        separately by ``GitSnapshotManager.revert()``; see the orchestrator's
        ``revert_to_snapshot()`` helper which chains both calls.
        """
        tables: list[tuple[str, bool]] = [
            ("tool_calls", True),
            ("errors", True),
            ("plans", True),
            ("decisions", True),
            ("messages", not keep_messages),
        ]

        deleted: Dict[str, int] = {}
        with self._lock:
            conn = self._get_connection()
            try:
                for table, do_delete in tables:
                    if do_delete:
                        cur = conn.execute(
                            f"DELETE FROM {table} WHERE session_id = ?",
                            (session_id,),
                        )
                        deleted[table] = cur.rowcount
                conn.commit()
                logger.info(f"SessionStore: reverted session '{session_id}': {deleted}")
            except Exception as e:
                conn.rollback()
                logger.error(f"SessionStore: revert_session failed: {e}")
                return {"ok": False, "error": str(e), "deleted": deleted}

        return {"ok": True, "deleted": deleted}
