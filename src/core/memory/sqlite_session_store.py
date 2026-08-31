from __future__ import annotations
import sqlite3
import json
import logging
import traceback
import threading
import uuid

# ruff: noqa: E501
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src.core.memory.sqlite_store_session_ops import (
    copy_missing_snapshot_rows,
    copy_session_rows,
    copy_session_snapshots,
)
from src.core.memory.sqlite_store_sidecar import (
    build_decision_records,
    build_write_failure_payload,
    read_decisions_sidecar,
    resolve_agent_context_dir,
    write_json_sidecar_with_fallback,
)
from src.core.memory.sqlite_store_queries import (
    base_summary,
    build_fts_mistake_query,
    build_like_search_params,
    build_like_search_sql,
    build_mistake_like_search_params,
    build_mistake_like_search_sql,
    build_mistake_search_params,
    build_mistake_search_sql,
    build_message_search_params,
    build_message_search_sql,
    build_recent_decisions_params,
    build_recent_decisions_sql,
    count_summary_fields,
    extract_session_ids,
    map_child_session_rows,
    map_error_rows,
    map_like_search_rows,
    map_mistake_rows,
    map_search_rows,
    map_tool_call_rows,
)
from src.core.memory.sqlite_store_collaborators import (
    ConnectionManager,
    SchemaManager,
    SnapshotManager,
)

logger = logging.getLogger(__name__)


class SqliteSessionStore:
    """SQLite-backed session store extracted for long-term memory use.

    This implementation is retained for archival/long-term storage use cases
    but is not used by default for ephemeral session persistence.

    Internal concerns are delegated to three collaborator objects:
    - ``_cm``  (ConnectionManager):  DB path resolution + connection lifecycle.
    - ``_sm``  (SchemaManager):      DDL, FTS setup, schema migrations.
    - ``_snap`` (SnapshotManager):   Snapshot save/get/revert.
    """

    _SCHEMA_VERSION = 4  # P3-T4: bumped from 3 to add session_plans table

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        # Use agent_context_path() for consistent behavior
        _agent_context_dir: Optional[Path] = None
        try:
            from src.tools.tools_config import agent_context_path

            _agent_context_dir = agent_context_path(self.workdir)
        except Exception:
            pass

        # Collaborators
        self._cm = ConnectionManager(
            workdir=self.workdir,
            agent_context_dir=_agent_context_dir,
        )
        self._sm = SchemaManager(
            conn_manager=self._cm,
            schema_version=self._SCHEMA_VERSION,
        )
        self._snap = SnapshotManager(
            conn_manager=self._cm,
            store_lock=self._cm._lock,
        )

        # Keep top-level aliases for backwards-compatible attribute access.
        self._lock = self._cm._lock
        self._local = self._cm._local
        self._session_locks = self._cm._session_locks
        self._session_locks_lock = self._cm._session_locks_lock
        self._thread_connections = self._cm._thread_connections
        self._thread_connections_lock = self._cm._thread_connections_lock

        # Historically the DB and tables were created at init-time. Some tests
        # expect the DB file to exist immediately after construction.
        try:
            self._get_writer_connection()
        except Exception:
            logger.debug(
                "SqliteSessionStore: eager DB creation failed during init\n%s",
                traceback.format_exc(),
            )

    # ------------------------------------------------------------------
    # Backwards-compatible shims that delegate to collaborators
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> Optional[Path]:
        return self._cm.db_path

    @db_path.setter
    def db_path(self, value: Optional[Path]) -> None:
        self._cm.db_path = value

    @property
    def _agent_context_dir(self) -> Optional[Path]:
        return self._cm._agent_context_dir

    @_agent_context_dir.setter
    def _agent_context_dir(self, value: Optional[Path]) -> None:
        self._cm._agent_context_dir = value

    def _resolve_db_path(self) -> Path:
        return self._cm.resolve_db_path()

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        return self._cm.get_session_lock(session_id)

    def _get_connection(self) -> sqlite3.Connection:
        return self._cm.get_connection()

    def _get_writer_connection(self) -> sqlite3.Connection:
        return self._cm.get_writer_connection(on_first_create=self._ensure_tables)

    def get_schema_version(self) -> int:
        return self._sm.get_schema_version()

    def _ensure_tables(self) -> None:
        self._sm.ensure_tables()

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        self._sm._run_migrations(conn)

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        self._sm._migrate_v2(conn)

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        self._sm._migrate_v3(conn)

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        self._sm._migrate_v4(conn)

    def _ensure_fts_index(self, conn: sqlite3.Connection) -> None:
        self._sm._ensure_fts_index(conn)

    def save_snapshot(
        self,
        session_id: str,
        state_json: str,
        role: Optional[str] = None,
        task: Optional[str] = None,
    ) -> Optional[str]:
        return self._snap.save_snapshot(session_id, state_json, role, task)

    def get_snapshot(self, session_id: str, snapshot_id: str) -> Optional[str]:
        return self._snap.get_snapshot(session_id, snapshot_id)

    def revert_session(
        self, session_id: str, keep_messages: Optional[object] = False
    ) -> Dict[str, Any]:
        return self._snap.revert_session(session_id, keep_messages)

    def _revert_session_keep_messages(
        self, session_id: str, keep_messages: bool = False
    ) -> Dict[str, Any]:
        return self._snap._revert_session_keep_messages(session_id, keep_messages)

    def close(self) -> None:
        self._cm.close()

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

    def iter_records(self, session_id: str) -> Iterator[Dict[str, Any]]:
        """Yield every message for *session_id* in chronological order.

        Sqlite-parity counterpart of ``JsonlSessionStore.iter_records``: a lazy
        generator that yields one message row at a time (bounded memory) rather
        than materialising the full session. Records are yield as
        ``{"session_id", "role", "content", "created_at"}`` dicts, matching the
        message representation the ``messages`` table stores.
        """
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT session_id, role, content, created_at "
            "FROM messages WHERE session_id=? ORDER BY created_at",
            (session_id or "unknown",),
        )
        for row in rows:
            yield {
                "session_id": row["session_id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }

    def read_page(
        self, session_id: str, page_size: int, offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Return a page of messages plus whether more records remain.

        Sqlite-parity counterpart of ``JsonlSessionStore.read_page``: a
        cursor/page API that materialises only the requested page (up to
        *page_size* messages) and reports an explicit ``has_more`` signal so
        callers paginate instead of silently dropping data.
        """
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        conn = self._get_connection()
        rows = conn.execute(
            "SELECT session_id, role, content, created_at "
            "FROM messages WHERE session_id=? ORDER BY created_at "
            "LIMIT ? OFFSET ?",
            (session_id or "unknown", page_size + 1, offset),
        ).fetchall()
        page = [
            {
                "session_id": r["session_id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"],
            }
            for r in rows[:page_size]
        ]
        has_more = len(rows) > page_size
        return page, has_more

    def search(
        self,
        query: str,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Full-text search using FTS5.

        Args:
            query: Search query (supports FTS5 operators like AND, OR, NOT)
            session_id: Optional session filter
            limit: Maximum results to return

        Returns:
            List of matching message dicts with session_id, role, content
        """
        if not query or not query.strip():
            return []

        conn = self._get_connection()

        try:
            sql, _ = build_message_search_sql(session_id=session_id)
            params = build_message_search_params(
                query=query,
                limit=limit,
                session_id=session_id,
            )
            rows = conn.execute(sql, params).fetchall()

            return map_search_rows(rows)
        except Exception as e:
            logger.warning(
                f"SqliteSessionStore: FTS search error: {e}; falling back to LIKE search"
            )
            return self._search_fallback(query, session_id=session_id, limit=limit)

    def _search_fallback(
        self,
        query: str,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fallback LIKE-based search when FTS is unavailable."""
        conn = self._get_connection()
        sql = build_like_search_sql(session_id=session_id)
        params = build_like_search_params(
            query=query,
            limit=limit,
            session_id=session_id,
        )
        rows = conn.execute(sql, params).fetchall()

        return map_like_search_rows(rows)

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
        return map_tool_call_rows(rows)

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

    # P3-T4: Cross-session plan resumption via session_plans table
    def save_plan(self, session_id: str, plan: list, task: str, step: int) -> None:
        """Persist the current plan to the session store for cross-session resumption."""
        import datetime

        self._execute_write(
            """INSERT OR REPLACE INTO session_plans
               (session_id, plan_json, task, current_step, saved_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                session_id or "unknown",
                json.dumps(plan, ensure_ascii=False),
                task or "",
                step,
                datetime.datetime.utcnow().isoformat(),
            ),
        )

    def load_plan(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load the most recently saved plan for cross-session resumption.

        Returns a dict with keys ``plan``, ``task``, ``current_step`` or ``None``.
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT plan_json, task, current_step FROM session_plans WHERE session_id = ?",
            (session_id or "unknown",),
        ).fetchone()
        if row:
            try:
                return {
                    "plan": json.loads(row["plan_json"]),
                    "task": row["task"],
                    "current_step": row["current_step"],
                }
            except Exception:
                return None
        return None

    # Error types that are transient / expected and should NOT be promoted to mistakes.
    _TRANSIENT_ERROR_TYPES: frozenset = frozenset(
        {"timeout", "permission_denied", "user_denied", "cancelled", "rate_limit"}
    )

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
        # Learning loop: auto-promote non-transient errors to the mistakes table
        # so they are available for cross-session FTS retrieval.
        if error_type not in self._TRANSIENT_ERROR_TYPES:
            try:
                self.add_mistake(
                    session_id=sid,
                    summary=f"{error_type}: {error_message[:100]}",
                    context=error_message[:400],
                    tool=None,
                )
            except Exception:
                pass  # never let mistake recording block error recording

    def get_errors(self, session_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT error_type, error_message, context FROM errors WHERE session_id=? ORDER BY created_at",
            (session_id or "unknown",),
        ).fetchall()
        return map_error_rows(rows)

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
                "SqliteSessionStore: write_decisions_json failed\n%s",
                traceback.format_exc(),
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

    # ------------------------------------------------------------------
    # Mistake retrieval (learning loop — FTS5 BM25)
    # ------------------------------------------------------------------

    def add_mistake(
        self,
        session_id: str,
        summary: str,
        context: Optional[str] = None,
        tool: Optional[str] = None,
    ) -> None:
        """Record a mistake for cross-session retrieval.

        Args:
            session_id: Current session identifier.
            summary:    Short description of what went wrong (used for FTS).
            context:    Optional surrounding context (e.g. tool args, error message).
            tool:       Optional tool name that produced the error.
        """
        self._execute_write(
            "INSERT INTO mistakes (session_id, summary, context, tool) VALUES (?, ?, ?, ?)",
            (session_id or "unknown", summary, context or "", tool or ""),
        )

    def search_mistakes(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Return up to *limit* past mistakes whose summary/context matches *query*.

        Uses FTS5 BM25 ranking when available; falls back to LIKE when not.
        Results are cross-session — this is intentionally workspace-global so
        the agent can learn from mistakes made in earlier sessions.

        Args:
            query: Natural-language query (e.g. the current task description).
            limit: Maximum number of results to return.

        Returns:
            List of dicts with keys: ``summary``, ``context``, ``tool``, ``ts``.
        """
        try:
            conn = self._get_connection()
            # Build an OR query from individual tokens so partial matches rank
            # instead of requiring all words to be present (FTS5 implicit AND).
            # Strip FTS5 special characters to avoid syntax errors.
            fts_query = build_fts_mistake_query(query)
            if not fts_query:
                return []
            rows = conn.execute(
                build_mistake_search_sql(),
                build_mistake_search_params(fts_query=fts_query, limit=limit),
            ).fetchall()
            return map_mistake_rows(rows)
        except Exception:
            pass

        # Fallback: LIKE search on summary
        try:
            conn = self._get_connection()
            rows = conn.execute(
                build_mistake_like_search_sql(),
                build_mistake_like_search_params(query=query, limit=limit),
            ).fetchall()
            return map_mistake_rows(rows)
        except Exception:
            return []

    def _write_with_retry(
        self,
        conn: Any,
        sql: str,
        params: tuple = (),
        session_id: Optional[str] = None,
        attempts: int = 5,
        base_backoff: float = 0.05,
    ) -> bool:
        """Retry a write against *conn* on SQLITE_BUSY/locked errors."""

        def write_operation():
            if hasattr(conn, "execute"):
                conn.execute(sql, params)
            if hasattr(conn, "commit"):
                conn.commit()
            return True

        # Store attempts on self so _write_diagnostic_on_failure can access it
        self._write_retry_attempts = attempts
        self._write_last_sql = sql
        self._write_last_params = params

        # Use shared utility for retry logic
        from src.core.memory._write_retry_utils import write_with_retry

        try:
            return write_with_retry(
                write_func=write_operation,
                max_attempts=attempts,
                base_delay=base_backoff,
                max_delay=1.0,
                context_msg="SqliteSessionStore",
            )
        except Exception as e:
            # Write diagnostic on failure (preserving original behavior)
            self._write_diagnostic_on_failure(session_id or "", e)
            return False

    def _write_diagnostic_on_failure(self, session_id: str, exc: Exception) -> None:
        try:
            import time

            ts = int(time.time() * 1000)
            sid = session_id or "unknown"
            diag_dir = resolve_agent_context_dir(
                workdir=self.workdir,
                agent_context_dir=getattr(self, "_agent_context_dir", None),
            )
            diag_dir.mkdir(parents=True, exist_ok=True)
            diag_path = diag_dir / f"session_store_write_failure_{ts}_{sid}.json"

            last_err = getattr(self, "_write_last_error", exc)
            payload = build_write_failure_payload(
                db_path=getattr(self, "db_path", None),
                session_id=sid,
                attempts=getattr(self, "_write_retry_attempts", 5),
                last_error=(
                    "SQLITE_BUSY/LOCKED"
                    if isinstance(last_err, sqlite3.OperationalError)
                    and "lock" in str(last_err).lower()
                    else str(last_err)
                ),
                sql=getattr(self, "_write_last_sql", "UNKNOWN"),
                params=getattr(self, "_write_last_params", ()),
                ts=ts,
            )
            write_json_sidecar_with_fallback(
                dest=diag_path,
                payload=payload,
                logger=logger,
                debug_prefix="SqliteSessionStore._write_with_retry: diagnostic",
            )
        except Exception:
            pass

    def write_decisions_json(self, limit: int = 50) -> None:
        """Collect recent decisions from the DB and write decisions.json sidecar.

        The most recent decisions are selected by created_at descending.
        """
        try:
            conn = self._get_connection()
            cur = conn.execute(
                build_recent_decisions_sql(),
                build_recent_decisions_params(limit),
            )
            rows = cur.fetchall()
            decisions = build_decision_records(rows)

            # For writes prefer the canonical agent-context resolver. This may
            # create the canonical directory and is appropriate at write-time.
            ac_dir = resolve_agent_context_dir(
                workdir=self.workdir,
                agent_context_dir=getattr(self, "_agent_context_dir", None),
            )
            dest = Path(ac_dir) / "decisions.json"
            write_json_sidecar_with_fallback(
                dest=dest,
                payload=decisions,
                logger=logger,
                debug_prefix="SqliteSessionStore.write_decisions_json",
            )
        except Exception:
            # Best-effort: do not propagate failures
            logger.debug(
                "SqliteSessionStore.write_decisions_json failed\n%s",
                traceback.format_exc(),
            )

    def read_recent_decisions(self, max_entries: int = 10) -> List[Dict[str, Any]]:
        """Read recent decisions from DB (preferred) or from decisions.json sidecar.

        Returns an empty list on error or when no decisions exist.
        """
        try:
            conn = self._get_connection()
            cur = conn.execute(
                build_recent_decisions_sql(),
                build_recent_decisions_params(max_entries),
            )
            rows = cur.fetchall()
            return build_decision_records(rows)
        except Exception:
            # Fallback to sidecar file
            try:
                path = resolve_agent_context_dir(
                    workdir=self.workdir,
                    agent_context_dir=getattr(self, "_agent_context_dir", None),
                )
                return read_decisions_sidecar(
                    path=Path(path) / "decisions.json",
                    max_entries=max_entries,
                )
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
        return map_child_session_rows(rows)

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

        def _build(sid: str, _visited: set | None = None) -> Dict[str, Any]:
            if _visited is None:
                _visited = set()
            if sid in _visited:
                logger.warning("get_session_tree: cycle detected at session_id=%r, stopping recursion", sid)
                return {"session_id": sid, "children": [], "cycle": True}
            _visited = _visited | {sid}
            children = []
            for ch in map_parent.get(str(sid), []):
                children.append(_build(ch, _visited))
            return {"session_id": sid, "children": children}

        return _build(session_id)

    def list_sessions(self) -> List[str]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM (SELECT session_id FROM messages UNION SELECT session_id FROM tool_calls UNION SELECT session_id FROM errors UNION SELECT session_id FROM plans UNION SELECT session_id FROM decisions)"
        ).fetchall()
        return extract_session_ids(rows)

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
                copy_session_rows(
                    conn=conn,
                    source_session_id=sid,
                    dest_session_id=new_id,
                )

                # Copy snapshots (preserve snapshot_id) and ensure the
                # associated row-level snapshot data is available for the
                # forked session. session_snapshot_rows is keyed by
                # snapshot_id so preserving the same snapshot_id means the
                # fork can use the same row-level data. To be defensive we
                # also insert any missing session_snapshot_rows rows (avoids
                # surprising gaps if the rows were stored separately).
                snapshot_ids = copy_session_snapshots(
                    conn=conn,
                    source_session_id=sid,
                    dest_session_id=new_id,
                )

                # Copy any missing session_snapshot_rows for the snapshot ids we
                # just attached to the fork. We check for an existing identical
                # (snapshot_id, table_name, rows_json) row before inserting to
                # avoid creating duplicates.
                try:
                    copy_missing_snapshot_rows(conn=conn, snapshot_ids=snapshot_ids)
                except Exception:
                    # Best-effort: if the snapshot rows query fails, ignore
                    pass

                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error("SqliteSessionStore.fork_session failed: %s", e)
                raise

        return new_id

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
        summary = base_summary(sid)
        for table_name, primary_key, secondary_key in count_summary_fields():
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table_name} WHERE session_id=?", (sid,)
                ).fetchone()
                count = int(row[0]) if row else 0
                summary[primary_key] = count
                summary[secondary_key] = count
            except Exception:
                pass
        return summary

    # P4-T3: Cross-session memory retrieval helpers
    def get_recent_sessions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the most recently active session IDs (by latest message).

        Returns a list of dicts with ``session_id`` and ``last_active`` keys.
        """
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """SELECT session_id, MAX(created_at) as last_active
                   FROM messages
                   GROUP BY session_id
                   ORDER BY last_active DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [{"session_id": r["session_id"], "last_active": r["last_active"]} for r in rows]
        except Exception:
            return []

    def get_session_text_summary(self, session_id: str, max_chars: int = 500) -> str:
        """Return a short human-readable text summary of a session.

        Pulls the first user message and the last assistant message as a proxy
        for what the session was about. Returns an empty string on any error.
        """
        conn = self._get_connection()
        try:
            first_user = conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY created_at ASC LIMIT 1",
                (session_id or "unknown",),
            ).fetchone()
            last_assistant = conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY created_at DESC LIMIT 1",
                (session_id or "unknown",),
            ).fetchone()
            parts = []
            if first_user:
                parts.append(f"Task: {first_user['content'][:200]}")
            if last_assistant:
                parts.append(f"Result: {last_assistant['content'][:200]}")
            return "; ".join(parts)[:max_chars]
        except Exception:
            return ""
