from __future__ import annotations
import sqlite3
import json
import logging
import traceback
import threading
import uuid

# ruff: noqa: E501
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from src.core.memory.sqlite_store_schema import (
    fts_creation_script,
    fts_trigger_statements,
    schema_creation_script,
    schema_version_from_row,
    serialise_snapshot_rows,
)
from src.core.memory.sqlite_store_session_ops import (
    copy_missing_snapshot_rows,
    copy_session_rows,
    copy_session_snapshots,
    delete_rows_after_snapshot,
    group_snapshot_rows,
    keep_messages_delete_specs,
    restore_snapshot_rows,
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

logger = logging.getLogger(__name__)


class SqliteSessionStore:
    """SQLite-backed session store extracted for long-term memory use.

    This implementation is retained for archival/long-term storage use cases
    but is not used by default for ephemeral session persistence.
    """

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        # Use agent_context_path() for consistent behavior
        try:
            from src.tools.tools_config import agent_context_path

            self._agent_context_dir = agent_context_path(self.workdir)
        except Exception:
            self._agent_context_dir = None
        # db_path is resolved lazily via _resolve_db_path when a connection is
        # required. Keep attribute for compatibility.
        self.db_path: Optional[Path] = None
        self._lock = threading.RLock()
        self._local = threading.local()
        self._writer_conn: Optional[sqlite3.Connection] = None
        self._thread_connections = {}
        self._thread_connections_lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_lock = threading.Lock()
        # Historically the DB and tables were created at init-time. Some tests
        # expect the DB file to exist immediately after construction. Prefer
        # lazy creation, but attempt an eager create here as a best-effort to
        # preserve test expectations while failing safely when the resolver
        # cannot run in the current environment.
        try:
            # This will resolve the path, create parent dirs and ensure tables.
            self._get_writer_connection()
        except Exception:
            # Best-effort: do not propagate initialization failures.
            logger.debug(
                "SqliteSessionStore: eager DB creation failed during init\n%s",
                traceback.format_exc(),
            )

    def _resolve_db_path(self) -> Path:
        """Resolve and cache the on-disk SQLite DB path.

        Resolution policy:
        - Prefer an already-discovered _agent_context_dir (legacy dirs may be
          discovered at init-time).
        - Otherwise prefer existing legacy locations: {workdir}/.agent-context
          then {workdir}/.agent.
        - If neither exists call the canonical resolver
          src.tools.tools_config.agent_context_path(workdir) at call-time.
          This may create the canonical directory and is appropriate for
          write-time resolution.
        The resolved Path is cached on self.db_path and self._agent_context_dir.
        """
        if getattr(self, "db_path", None) is not None:
            return cast(Path, self.db_path)

        # If an agent-context dir was discovered earlier prefer it
        ac = getattr(self, "_agent_context_dir", None)
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
                # Use agent_context_path() when available; otherwise fall back to
                # the canonical on-disk default instead of referencing an unbound
                # import target in the failure branch.
                try:
                    from src.tools.tools_config import agent_context_path
                    ac_path = agent_context_path(self.workdir)
                except Exception:
                    ac_path = self.workdir / ".codingAgent"

        # Cache resolved values
        self._agent_context_dir = ac_path
        self.db_path = ac_path / "session.db"
        return self.db_path

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
        # Ensure we have a resolved DB path (may call canonical resolver at
        # call-time). This mirrors the existence-first policy used elsewhere.
        dbp = self._resolve_db_path()

        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(str(dbp), timeout=30.0)
            self._local.connection = conn
            try:
                with self._thread_connections_lock:
                    self._thread_connections[threading.get_ident()] = conn
            except Exception:
                logger.debug(
                    "SqliteSessionStore: failed to register thread connection\n%s",
                    traceback.format_exc(),
                )
            self._local.connection.row_factory = sqlite3.Row
            self._local.connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection.execute("PRAGMA busy_timeout=1000")
        return self._local.connection

    def _get_writer_connection(self) -> sqlite3.Connection:
        if self._writer_conn is None:
            dbp = self._resolve_db_path()
            # Ensure parent directory exists for the file-based DB
            dbp.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(dbp), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=1000")
            self._writer_conn = conn
            # Ensure required tables exist on first writer creation.
            try:
                self._ensure_tables()
            except Exception:
                # Best-effort: if table creation fails, log and continue so
                # caller receives the writer connection and can observe/raise
                logger.debug(
                    "SqliteSessionStore: _ensure_tables failed\n%s",
                    traceback.format_exc(),
                )
        return self._writer_conn

    _SCHEMA_VERSION = 3

    def get_schema_version(self) -> int:
        """Return the schema version for compatibility with other stores."""
        try:
            return int(self._SCHEMA_VERSION)
        except Exception:
            return 1

    def _ensure_tables(self):
        # Resolve DB path and ensure tables exist. Writer connection will
        # create parent directories as needed.
        _ = self._resolve_db_path()
        conn = self._get_writer_connection()
        try:
            conn.executescript(schema_creation_script())

            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(self._SCHEMA_VERSION),),
            )
            conn.commit()

            self._ensure_fts_index(conn)
            self._run_migrations(conn)

        except Exception as e:
            logger.error(f"SqliteSessionStore: failed to create tables: {e}")

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Run schema migrations from current version to target version."""
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_version = schema_version_from_row(row)
        except Exception:
            current_version = 1

        if current_version >= self._SCHEMA_VERSION:
            return

        logger.info(
            f"SqliteSessionStore: migrating from v{current_version} to v{self._SCHEMA_VERSION}"
        )

        if current_version < 2 and self._SCHEMA_VERSION >= 2:
            self._migrate_v2(conn)
            current_version = 2

        if current_version < 3 and self._SCHEMA_VERSION >= 3:
            self._migrate_v3(conn)
            current_version = 3

        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(self._SCHEMA_VERSION),),
        )
        conn.commit()
        logger.info(
            f"SqliteSessionStore: migration complete to v{self._SCHEMA_VERSION}"
        )

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """Migration from v1 to v2: Add session_children table if not exists."""
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
                """
                CREATE INDEX IF NOT EXISTS idx_children_parent ON session_children(parent_session_id);
            """
            )
            logger.debug("SqliteSessionStore: v2 migration complete")
        except Exception as e:
            logger.warning(f"SqliteSessionStore: v2 migration failed: {e}")

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        """Migration from v2 to v3: Add mistakes table and FTS index."""
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
            logger.debug("SqliteSessionStore: v3 migration complete")
        except Exception as e:
            logger.warning(f"SqliteSessionStore: v3 migration failed: {e}")

    def _ensure_fts_index(self, conn: sqlite3.Connection) -> None:
        """Create and maintain FTS5 full-text search index."""
        try:
            conn.executescript(fts_creation_script())
            for statement in fts_trigger_statements():
                conn.execute(statement)

            conn.commit()
            logger.debug("SqliteSessionStore: FTS5 index ready")
        except Exception as e:
            logger.warning(f"SqliteSessionStore: FTS index creation failed: {e}")

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
            self._write_diagnostic_on_failure(session_id, e)
            return False

    def _write_diagnostic_on_failure(self, session_id: str, exc: Exception) -> None:
        """Write diagnostic information on write failure for debugging."""
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
    ) -> Optional[str]:
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
                        serialised = serialise_snapshot_rows(rows)
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
                return None

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

                            deleted = restore_snapshot_rows(
                                conn=wconn,
                                session_id=sid,
                                grouped_rows=group_snapshot_rows(rows),
                                initial_deleted=deleted,
                            )

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
                tables = keep_messages_delete_specs(keep_messages)
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
                    "SqliteSessionStore.close: failed to close thread connection %s\n%s",
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
                            "SqliteSessionStore.close: failed to close writer connection\n%s",
                            traceback.format_exc(),
                        )
                    finally:
                        self._writer_conn = None
        except Exception:
            logger.debug(
                "SqliteSessionStore.close: unexpected error during close\n%s",
                traceback.format_exc(),
            )
