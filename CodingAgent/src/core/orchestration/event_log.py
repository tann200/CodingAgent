"""event_log.py — Immutable SQLite append-only event log (S4-B).

Provides a lightweight, structured audit trail for every significant agent
action: tool calls, file writes, LLM turns, session lifecycle events, and
snapshot hashes.

Design
------
- **SQLite** — zero external dependencies, crash-safe with WAL mode,
  supports concurrent readers / single writer.
- **Append-only** — events are never updated or deleted via the public API.
  The schema has no UPDATE triggers; callers must use ``append()`` only.
- **Typed events** — ``EventKind`` enum + a JSON ``data`` blob for structured
  payloads.
- **Thread-safe** — uses ``threading.Lock`` around every write so multiple
  threads can safely call ``append()``.  Reads do NOT acquire the lock (SQLite
  WAL handles concurrent read isolation).
- **Diff helper** — ``get_diff(session_id, from_seq, to_seq)`` returns all
  tool-call events between two sequence numbers, enabling the ``/diff`` TUI
  command (S5-C) to reconstruct what changed during a session.

Schema::

    events(
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT    NOT NULL,
        seq        INTEGER NOT NULL,       -- per-session monotonic counter
        kind       TEXT    NOT NULL,       -- EventKind value
        timestamp  REAL    NOT NULL,       -- Unix timestamp (float)
        data       TEXT    NOT NULL,       -- JSON-encoded payload
        snapshot   TEXT                    -- optional git tree hash
    )

Usage::

    from pathlib import Path
    from src.core.orchestration.event_log import EventLog, EventKind
    from src.core.paths import get_events_db_path

    log = EventLog(db_path=get_events_db_path())
    log.append(session_id="s1", kind=EventKind.TOOL_CALL,
               data={"tool": "read_file", "args": {"path": "foo.py"}})
    events = log.get_events(session_id="s1")
    diff_events = log.get_diff(session_id="s1", from_seq=0, to_seq=10)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EventKind
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    """Canonical event types stored in the event log."""

    # Session lifecycle
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    SESSION_FORK = "session.fork"
    SESSION_REVERT = "session.revert"

    # LLM interactions
    LLM_TURN_START = "llm.turn_start"
    LLM_TURN_END = "llm.turn_end"
    LLM_COMPACTION = "llm.compaction"

    # Tool execution
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"
    TOOL_APPROVED = "tool.approved"
    TOOL_DENIED = "tool.denied"

    # File operations (high-level summary)
    FILE_WRITE = "file.write"
    FILE_DELETE = "file.delete"

    # Snapshot markers
    SNAPSHOT_TAKEN = "snapshot.taken"

    # Orchestration
    PLAN_ENTER = "plan.enter"
    PLAN_EXIT = "plan.exit"
    SUBAGENT_SPAWN = "subagent.spawn"
    LOOP_DETECTED = "loop.detected"

    # Generic / user-defined
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ---------------------------------------------------------------------------
# EventRecord
# ---------------------------------------------------------------------------


@dataclass
class EventRecord:
    """A single event row retrieved from the log."""

    id: int
    session_id: str
    seq: int
    kind: str
    timestamp: float
    data: Dict[str, Any]
    snapshot: Optional[str] = None

    @classmethod
    def from_row(cls, row: tuple) -> "EventRecord":
        id_, session_id, seq, kind, ts, data_json, snapshot = row
        return cls(
            id=id_,
            session_id=session_id,
            seq=seq,
            kind=kind,
            timestamp=ts,
            data=json.loads(data_json) if data_json else {},
            snapshot=snapshot,
        )


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------


class EventLog:
    """Append-only SQLite event log.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Created (with parent dirs) if it
        does not exist.  Use ``Path(":memory:")`` for in-process tests — but
        note that ``":memory:"`` databases are not shared across connections,
        so each ``EventLog()`` instance will have its own isolated DB.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._seq: Dict[str, int] = {}  # session_id → last seq number
        self._conn: Optional[sqlite3.Connection] = None
        self._init()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(
        self,
        session_id: str,
        kind: "EventKind | str",
        data: Optional[Dict[str, Any]] = None,
        snapshot: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> int:
        """Append a new event and return its ``seq`` number.

        Parameters
        ----------
        session_id:
            The session this event belongs to.
        kind:
            One of ``EventKind`` or an arbitrary string for forward-compat.
        data:
            Structured payload (must be JSON-serialisable).
        snapshot:
            Optional git tree hash from the snapshot manager.
        timestamp:
            Unix timestamp; defaults to ``time.time()``.

        Returns
        -------
        int
            The per-session sequence number assigned to this event.
        """
        kind_str = kind.value if isinstance(kind, EventKind) else kind
        ts = timestamp if timestamp is not None else time.time()
        data_json = json.dumps(data or {}, default=str)

        with self._lock:
            seq = self._seq.get(session_id, 0) + 1
            self._seq[session_id] = seq
            if self._conn is None:
                raise RuntimeError("EventLog: database connection is not open")
            self._conn.execute(
                "INSERT INTO events (session_id, seq, kind, timestamp, data, snapshot)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, seq, kind_str, ts, data_json, snapshot),
            )
            self._conn.commit()

        logger.debug("event_log[%s]: %s seq=%d", session_id, kind_str, seq)
        return seq

    def get_events(
        self,
        session_id: str,
        kind: Optional["EventKind | str"] = None,
        from_seq: int = 0,
        to_seq: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[EventRecord]:
        """Return events for *session_id* ordered by seq ascending.

        Parameters
        ----------
        session_id:
            Filter by session.
        kind:
            If provided, filter to this ``EventKind`` (or string kind).
        from_seq:
            Return events with seq > *from_seq* (exclusive lower bound).
        to_seq:
            Return events with seq <= *to_seq* (inclusive upper bound).
            ``None`` means no upper bound.
        limit:
            Maximum number of events to return.
        """
        if self._conn is None:
            raise RuntimeError("EventLog: database connection is not open")
        kind_str: Optional[str] = None
        if kind is not None:
            kind_str = kind.value if isinstance(kind, EventKind) else kind

        sql = "SELECT id, session_id, seq, kind, timestamp, data, snapshot FROM events WHERE session_id = ? AND seq > ?"
        params: list = [session_id, from_seq]

        if to_seq is not None:
            sql += " AND seq <= ?"
            params.append(to_seq)

        if kind_str is not None:
            sql += " AND kind = ?"
            params.append(kind_str)

        sql += " ORDER BY seq ASC"

        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        rows = self._conn.execute(sql, params).fetchall()
        return [EventRecord.from_row(r) for r in rows]

    def get_diff(
        self,
        session_id: str,
        from_seq: int = 0,
        to_seq: Optional[int] = None,
    ) -> List[EventRecord]:
        """Return tool-call and file-write events between *from_seq* and *to_seq*.

        This is the building block for the ``/diff`` TUI command (S5-C): it
        shows what the agent *did* between two points in time.
        """
        if self._conn is None:
            raise RuntimeError("EventLog: database connection is not open")
        diff_kinds = (
            EventKind.TOOL_CALL.value,
            EventKind.FILE_WRITE.value,
            EventKind.FILE_DELETE.value,
            EventKind.SNAPSHOT_TAKEN.value,
        )
        placeholders = ",".join("?" * len(diff_kinds))
        sql = (
            f"SELECT id, session_id, seq, kind, timestamp, data, snapshot"
            f" FROM events"
            f" WHERE session_id = ? AND seq > ? AND kind IN ({placeholders})"
        )
        params: list = [session_id, from_seq, *diff_kinds]
        if to_seq is not None:
            sql += " AND seq <= ?"
            params.append(to_seq)
        sql += " ORDER BY seq ASC"
        rows = self._conn.execute(sql, params).fetchall()
        return [EventRecord.from_row(r) for r in rows]

    def get_last_seq(self, session_id: str) -> int:
        """Return the highest seq for *session_id*, or 0 if none."""
        if self._conn is None:
            raise RuntimeError("EventLog: database connection is not open")
        row = self._conn.execute(
            "SELECT MAX(seq) FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        val = row[0] if row else None
        return int(val) if val is not None else 0

    def count(self, session_id: str) -> int:
        """Return total event count for *session_id*."""
        if self._conn is None:
            raise RuntimeError("EventLog: database connection is not open")
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init(self) -> None:
        """Create the database and schema if needed."""
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,  # We handle locking ourselves
        )
        # WAL mode: safe concurrent reads while writing
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                seq        INTEGER NOT NULL,
                kind       TEXT    NOT NULL,
                timestamp  REAL    NOT NULL,
                data       TEXT    NOT NULL DEFAULT '{}',
                snapshot   TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_session_seq"
            " ON events (session_id, seq)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind)"
        )
        self._conn.commit()
        # Populate in-memory seq cache from existing data
        rows = self._conn.execute(
            "SELECT session_id, MAX(seq) FROM events GROUP BY session_id"
        ).fetchall()
        for session_id, max_seq in rows:
            self._seq[session_id] = int(max_seq)
        logger.debug(
            "event_log: opened %s (%d session(s) cached)", self._db_path, len(self._seq)
        )
