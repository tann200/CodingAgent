from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def build_message_search_sql(*, session_id: Optional[str]) -> Tuple[str, tuple]:
    if session_id:
        return (
            """
                    SELECT m.session_id, m.role, m.content, rank
                    FROM messages_fts fts
                    JOIN messages m ON m.rowid = fts.rowid
                    WHERE messages_fts MATCH ?
                      AND m.session_id = ?
                    ORDER BY rank
                    LIMIT ?
                """,
            (),
        )
    return (
        """
                SELECT m.session_id, m.role, m.content, rank
                FROM messages_fts fts
                JOIN messages m ON m.rowid = fts.rowid
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """,
        (),
    )


def build_message_search_params(
    *,
    query: str,
    limit: int,
    session_id: Optional[str],
) -> tuple:
    if session_id:
        return (query, session_id, limit)
    return (query, limit)


def build_like_search_sql(*, session_id: Optional[str]) -> str:
    if session_id:
        return """
                SELECT session_id, role, content
                FROM messages
                WHERE session_id = ? AND content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """
    return """
                SELECT session_id, role, content
                FROM messages
                WHERE content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """


def build_like_search_params(
    *,
    query: str,
    limit: int,
    session_id: Optional[str],
) -> tuple:
    pattern = f"%{query}%"
    if session_id:
        return (session_id, pattern, limit)
    return (pattern, limit)


def build_mistake_search_sql() -> str:
    return """
                SELECT m.summary, m.context, m.tool, m.created_at
                FROM mistakes_fts fts
                JOIN mistakes m ON m.rowid = fts.rowid
                WHERE mistakes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """


def build_mistake_search_params(*, fts_query: str, limit: int) -> tuple:
    return (fts_query, limit)


def build_mistake_like_search_sql() -> str:
    return """
                SELECT summary, context, tool, created_at
                FROM mistakes
                WHERE summary LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """


def build_mistake_like_search_params(*, query: str, limit: int) -> tuple:
    return (f"%{query[:80]}%", limit)


def build_recent_decisions_sql() -> str:
    return (
        "SELECT session_id, decision, rationale, created_at FROM decisions "
        "ORDER BY created_at DESC LIMIT ?"
    )


def build_recent_decisions_params(limit: int) -> tuple:
    return (int(limit),)


def map_search_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
            "rank": row.get("rank") if hasattr(row, "get") else row["rank"],
        }
        for row in rows
    ]


def map_like_search_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
        }
        for row in rows
    ]


def decode_json_field(value: Any) -> Any:
    try:
        return json.loads(value) if value is not None else None
    except Exception:
        return value


def map_tool_call_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "tool_name": row["tool_name"],
            "args": decode_json_field(row["args"]),
            "result": decode_json_field(row["result"]),
            "success": bool(row["success"] or 0),
        }
        for row in rows
    ]


def map_error_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "error_type": row["error_type"],
            "error_message": row["error_message"],
            "context": decode_json_field(row["context"]),
        }
        for row in rows
    ]


def map_child_session_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "parent_session_id": row["parent_session_id"],
            "child_session_id": row["child_session_id"],
            "role": row["role"],
            "task": row["task"],
        }
        for row in rows
    ]


def map_mistake_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "summary": row["summary"],
            "context": row["context"],
            "tool": row["tool"],
            "ts": row["created_at"],
        }
        for row in rows
    ]


def build_fts_mistake_query(query: str, *, max_tokens: int = 20) -> str:
    tokens = re.sub(r"[^\w\s]", " ", query).split()
    if not tokens:
        return ""
    return " OR ".join(tokens[:max_tokens])


def extract_session_ids(rows: Sequence[Any]) -> List[str]:
    return sorted({row[0] for row in rows if row[0] is not None})


def count_summary_fields() -> Sequence[Tuple[str, str, str]]:
    return (
        ("messages", "messages", "message_count"),
        ("tool_calls", "tool_calls", "tool_call_count"),
        ("errors", "errors", "error_count"),
        ("plans", "plans", "plans"),
        ("decisions", "decisions", "decisions"),
    )


def base_summary(session_id: str) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "messages": 0,
        "message_count": 0,
        "tool_calls": 0,
        "tool_call_count": 0,
        "errors": 0,
        "error_count": 0,
        "plans": 0,
        "decisions": 0,
    }
