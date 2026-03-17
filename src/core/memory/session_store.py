from __future__ import annotations
import sqlite3
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class SessionStore:
    """SQLite-based session store for conversation retrieval and debugging."""

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.db_path = self.workdir / ".agent-context" / "session.db"
        self._ensure_tables()

    def _ensure_tables(self):
        """Create tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
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
        finally:
            conn.close()

    def add_message(self, session_id: str, role: str, content: str):
        """Add a message to the session."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            conn.commit()
        finally:
            conn.close()

    def get_messages(self, session_id: str, limit: int = 100) -> List[Dict]:
        """Get messages for a session."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                "SELECT id, role, content, created_at FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            )
            return [
                {"id": row[0], "role": row[1], "content": row[2], "created_at": row[3]}
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def add_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: Dict,
        result: Any = None,
        success: bool = True,
    ):
        """Record a tool call."""
        conn = sqlite3.connect(str(self.db_path))
        try:
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
        finally:
            conn.close()

    def get_tool_calls(self, session_id: str, limit: int = 100) -> List[Dict]:
        """Get tool calls for a session."""
        conn = sqlite3.connect(str(self.db_path))
        try:
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
        finally:
            conn.close()

    def add_error(
        self, session_id: str, error_type: str, error_message: str, context: Dict = None
    ):
        """Record an error."""
        conn = sqlite3.connect(str(self.db_path))
        try:
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
        finally:
            conn.close()

    def get_errors(self, session_id: str, limit: int = 50) -> List[Dict]:
        """Get errors for a session."""
        conn = sqlite3.connect(str(self.db_path))
        try:
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
        finally:
            conn.close()

    def add_plan(self, session_id: str, plan: str, status: str = "active"):
        """Add a plan."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO plans (session_id, plan, status) VALUES (?, ?, ?)",
                (session_id, plan, status),
            )
            conn.commit()
        finally:
            conn.close()

    def get_plans(self, session_id: str) -> List[Dict]:
        """Get plans for a session."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                "SELECT id, plan, status, created_at FROM plans WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            )
            return [
                {"id": row[0], "plan": row[1], "status": row[2], "created_at": row[3]}
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def add_decision(self, session_id: str, decision: str, rationale: str = None):
        """Record a decision."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO decisions (session_id, decision, rationale) VALUES (?, ?, ?)",
                (session_id, decision, rationale),
            )
            conn.commit()
        finally:
            conn.close()

    def get_decisions(self, session_id: str) -> List[Dict]:
        """Get decisions for a session."""
        conn = sqlite3.connect(str(self.db_path))
        try:
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
        finally:
            conn.close()

    def list_sessions(self) -> List[str]:
        """List all unique session IDs."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                "SELECT DISTINCT session_id FROM messages ORDER BY created_at DESC"
            )
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_session_summary(self, session_id: str) -> Dict:
        """Get a summary of a session."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            msg_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            tool_count = conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE session_id = ?", (session_id,)
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
        finally:
            conn.close()
