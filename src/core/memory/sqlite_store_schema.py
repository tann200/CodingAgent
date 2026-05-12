from __future__ import annotations

from typing import Any, Dict, List


def schema_creation_script() -> str:
    return """
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
                CREATE TABLE IF NOT EXISTS mistakes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    context TEXT,
                    tool TEXT,
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
                CREATE TABLE IF NOT EXISTS session_plans (
                    session_id TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL,
                    task TEXT,
                    current_step INTEGER DEFAULT 0,
                    saved_at TEXT
                );
            """


def fts_creation_script() -> str:
    return """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    session_id,
                    role,
                    content,
                    tokenize='porter unicode61'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS mistakes_fts USING fts5(
                    session_id,
                    summary,
                    context,
                    tool,
                    tokenize='porter unicode61'
                );
            """


def fts_trigger_statements() -> List[str]:
    return [
        """
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts (session_id, role, content)
                    VALUES (new.session_id, new.role, new.content);
                END;
            """,
        """
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    DELETE FROM messages_fts WHERE rowid IN (
                        SELECT rowid FROM messages_fts WHERE session_id = old.session_id
                        AND content = old.content LIMIT 1
                    );
                END;
            """,
        """
                CREATE TRIGGER IF NOT EXISTS mistakes_ai AFTER INSERT ON mistakes BEGIN
                    INSERT INTO mistakes_fts (session_id, summary, context, tool)
                    VALUES (new.session_id, new.summary, new.context, new.tool);
                END;
            """,
    ]


def schema_version_from_row(row: Any, default: int = 1) -> int:
    try:
        return int(row["value"]) if row else default
    except Exception:
        return default


def serialise_snapshot_rows(rows: List[Any]) -> List[Dict[str, Any]]:
    serialised: List[Dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data.pop("id", None)
        data.pop("session_id", None)
        serialised.append(data)
    return serialised
