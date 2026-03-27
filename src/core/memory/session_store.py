from __future__ import annotations
import sqlite3
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class SessionStore:
    """SQLite-based session store for conversation retrieval and debugging."""

    def __init__(self, workdir: str = None):
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
                
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
                CREATE INDEX IF NOT EXISTS idx_errors_session ON errors(session_id);
            """)
            conn.commit()
        except Exception as e:
            logger.error(f"SessionStore: failed to create tables: {e}")

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
        self, session_id: str, error_type: str, error_message: str, context: Dict = None
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

    def add_decision(self, session_id: str, decision: str, rationale: str = None):
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
