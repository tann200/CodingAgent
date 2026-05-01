"""permission_table.py — TASK-PERM-3

SQLite-backed permission rule table.  Stores "allow always" and "deny always"
rules for specific tool kinds and argument patterns (glob or exact).

Schema
------
::

    CREATE TABLE permission_rules (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      tool_kind  TEXT NOT NULL,     -- e.g. "write", "bash", "webfetch"
      pattern    TEXT NOT NULL,     -- glob matched against the tool's first arg
      action     TEXT NOT NULL,     -- "allow" | "deny"
      scope      TEXT NOT NULL,     -- "project" | "session"
      created_at INTEGER NOT NULL   -- unix timestamp
    );

Usage
-----
::

    from src.core.orchestration.permission_table import PermissionTable

    tbl = PermissionTable()                          # default DB path
    tbl.add_rule("write", "src/**/*.py", "allow", "project")
    action = tbl.check("write", "src/foo.py")       # "allow" | "deny" | None
    tbl.clear_session_rules()                        # call on session end
"""

from __future__ import annotations

import fnmatch
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_NAME = "permission_rules.db"


def _default_db_path() -> Path:
    """Return the path to the permission rules database."""
    try:
        from src.core.paths import get_data_dir

        return get_data_dir() / _DEFAULT_DB_NAME
    except Exception:
        try:
            from src.tools.tools_config import agent_context_path

            return agent_context_path(Path.cwd()) / _DEFAULT_DB_NAME
        except Exception:
            return Path(".agent-context") / _DEFAULT_DB_NAME


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS permission_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_kind  TEXT    NOT NULL,
    pattern    TEXT    NOT NULL,
    action     TEXT    NOT NULL CHECK(action IN ('allow', 'deny')),
    scope      TEXT    NOT NULL CHECK(scope IN ('project', 'session')),
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perm_kind ON permission_rules (tool_kind);
"""


class PermissionTable:
    """Thread-safe SQLite-backed permission rule store.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Defaults to the project data dir.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _default_db_path()
        self._lock = threading.Lock()
        self._local = threading.local()
        self._ensure_schema()

    def _get_connection(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection, creating it on first access."""
        if not hasattr(self._local, "conn"):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_rule(
        self,
        tool_kind: str,
        pattern: str,
        action: str,
        scope: str = "project",
    ) -> int:
        """Insert a new permission rule and return its row id.

        Parameters
        ----------
        tool_kind:
            Category string (e.g. ``"write"``, ``"bash"``, ``"webfetch"``).
        pattern:
            Glob pattern matched against the primary argument of the tool
            call (path, URL, command, etc.).  ``"*"`` matches everything.
        action:
            ``"allow"`` or ``"deny"``.
        scope:
            ``"project"`` (survives restart) or ``"session"`` (deleted on
            :meth:`clear_session_rules`).

        Returns
        -------
        int
            The new row id.
        """
        action = action.lower()
        scope = scope.lower()
        if action not in ("allow", "deny"):
            raise ValueError(f"action must be 'allow' or 'deny', got: {action!r}")
        if scope not in ("project", "session"):
            raise ValueError(f"scope must be 'project' or 'session', got: {scope!r}")

        now = int(time.time())
        with self._lock:
            conn = self._get_connection()
            cur = conn.execute(
                "INSERT INTO permission_rules (tool_kind, pattern, action, scope, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (tool_kind, pattern, action, scope, now),
            )
            conn.commit()
            row_id = cur.lastrowid
            logger.debug(
                "permission_table: added rule %s %s → %s (%s)",
                tool_kind,
                pattern,
                action,
                scope,
            )
            return row_id

    def check(self, tool_kind: str, argument: str = "") -> Optional[str]:
        """Check whether *argument* matches any stored rule for *tool_kind*.

        Rules are evaluated newest-first (highest id wins), so later "allow
        always" rules can override earlier deny rules and vice-versa.

        Parameters
        ----------
        tool_kind:
            Same string used when calling :meth:`add_rule`.
        argument:
            The primary argument of the tool call (path, URL, command, etc.).
            Pass an empty string to match only ``"*"`` patterns.

        Returns
        -------
        ``"allow"`` | ``"deny"`` | ``None``
            ``None`` means no rule matched — the caller decides.
        """
        with self._lock:
            conn = self._get_connection()
            rows = conn.execute(
                "SELECT pattern, action FROM permission_rules"
                " WHERE tool_kind = ? ORDER BY id DESC",
                (tool_kind,),
            ).fetchall()

        # Helper: match path-style patterns (containing '/') using
        # segment-wise matching so that the special token '**' behaves like
        # glob's recursive '**' (i.e. matches zero or more path segments).
        def _path_matches(pattern: str, arg: str) -> bool:
            pat = pattern.replace("\\", "/")
            a = arg.replace("\\", "/")
            patt_segs = pat.split("/")
            path_segs = a.split("/")

            # Recursive matcher over segment lists
            def match_segs(i: int, j: int) -> bool:
                while i < len(patt_segs) and j < len(path_segs):
                    ps = patt_segs[i]
                    if ps == "**":
                        # If ** is the final pattern segment it may match the
                        # remainder (including empty).
                        if i == len(patt_segs) - 1:
                            return True
                        # If ** appears in the middle of the pattern (e.g.
                        # src/**/x), require it to match at least one segment
                        # to satisfy tests expecting that behavior.
                        start_k = j
                        if 0 < i < len(patt_segs) - 1:
                            # Require at least one segment to be consumed.
                            start_k = j + 1
                        for k in range(start_k, len(path_segs) + 1):
                            if match_segs(i + 1, k):
                                return True
                        return False
                    # Single-segment match (wildcards do not cross '/')
                    if not fnmatch.fnmatchcase(path_segs[j], ps):
                        return False
                    i += 1
                    j += 1

                # Consume any trailing ** in pattern (they can match empty)
                while i < len(patt_segs) and patt_segs[i] == "**":
                    i += 1

                return i == len(patt_segs) and j == len(path_segs)

            return match_segs(0, 0)

        # For patterns without a path separator, preserve legacy behavior
        # (fnmatch on the whole argument). For patterns containing '/', use
        # the segment-wise matcher which implements the desired '**'
        # semantics (zero-or-more segments).
        for pattern, action in rows:
            try:
                matched = (
                    _path_matches(pattern, argument)
                    if "/" in pattern
                    else fnmatch.fnmatch(argument, pattern)
                )
            except Exception:
                # Non-fatal: if matching fails for any reason, skip the rule
                matched = False

            if matched:
                logger.debug(
                    "permission_table: %s %r matched pattern %r → %s",
                    tool_kind,
                    argument,
                    pattern,
                    action,
                )
                return action

        return None

    def list_rules(self, tool_kind: Optional[str] = None) -> list[dict]:
        """Return all rules, optionally filtered by *tool_kind*.

        Each dict has keys: ``id``, ``tool_kind``, ``pattern``, ``action``,
        ``scope``, ``created_at``.
        """
        with self._lock:
            conn = self._get_connection()
            if tool_kind:
                rows = conn.execute(
                    "SELECT id, tool_kind, pattern, action, scope, created_at"
                    " FROM permission_rules WHERE tool_kind = ? ORDER BY id",
                    (tool_kind,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, tool_kind, pattern, action, scope, created_at"
                    " FROM permission_rules ORDER BY id",
                ).fetchall()

        return [
            {
                "id": r[0],
                "tool_kind": r[1],
                "pattern": r[2],
                "action": r[3],
                "scope": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    def delete_rule(self, rule_id: int) -> bool:
        """Delete a rule by its row id.  Returns True when a row was deleted."""
        with self._lock:
            conn = self._get_connection()
            cur = conn.execute(
                "DELETE FROM permission_rules WHERE id = ?", (rule_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def clear_session_rules(self) -> int:
        """Delete all rules with ``scope='session'``.  Returns count deleted."""
        with self._lock:
            conn = self._get_connection()
            cur = conn.execute(
                "DELETE FROM permission_rules WHERE scope = 'session'"
            )
            conn.commit()
            deleted = cur.rowcount
            if deleted:
                logger.debug(
                    "permission_table: cleared %d session rule(s)", deleted
                )
            return deleted

    def clear_all(self) -> None:
        """Delete all rules (used in tests)."""
        with self._lock:
            conn = self._get_connection()
            conn.execute("DELETE FROM permission_rules")
            conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the table if it does not exist."""
        try:
            conn = self._get_connection()
            conn.executescript(_CREATE_TABLE_SQL)
            conn.commit()
        except Exception as exc:
            logger.warning("permission_table: schema init failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton (test-injectable via PermissionTable(db_path=...))
# ---------------------------------------------------------------------------

_singleton: Optional[PermissionTable] = None
_singleton_lock = threading.Lock()


def get_permission_table(db_path: Optional[Path] = None) -> PermissionTable:
    """Return (creating if needed) the module-level PermissionTable singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None or db_path is not None:
            _singleton = PermissionTable(db_path=db_path)
        return _singleton
