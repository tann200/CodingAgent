"""jsonl_session_store.py — Append-only JSONL session store (TASK-7).

A drop-in alternative to the SQLite-backed ``SessionStore`` that satisfies
``SessionStoreProtocol`` (defined in ``src/core/interfaces.py``).

Design goals
------------
- **Append-only**: writes go only to the end of a ``.jsonl`` file; no UPDATE
  or DELETE.  This makes crash-safety trivial — a partial final line is simply
  ignored on read.
- **Single-file per session**: ``{workdir}/.codingAgent/sessions/{session_id}.jsonl``
- **Rotation at 256 KB**: when a session file exceeds ``_ROTATION_BYTES`` the
  current file is renamed to ``{session_id}.{n}.jsonl`` and a fresh file
  is opened.  Reads reconstruct the full history by scanning all rotated files
  in order.
- **Fork via copy**: ``fork_session(source, new)`` copies the source JSONL
  files byte-for-byte into ``new.*`` files.  Since files are append-only the
  copy is atomic enough for our purposes.
- **Revert via byte-offset snapshot**: ``save_snapshot`` records the current
  file path + byte offset; ``revert_session`` truncates the file to that
  offset, effectively rolling back all appends since the snapshot.
- **Thread-safe**: a per-instance ``threading.Lock`` serialises all writes
  to the same session; separate sessions are independent.

File format
-----------
Each line is a JSON object with at least a ``"type"`` key::

    {"type": "message", "role": "user", "content": "hello", "ts": "..."}
    {"type": "message", "role": "assistant", "content": "hi", "ts": "..."}

Snapshot records written inline::

    {"type": "snapshot", "snapshot_id": "...", "state_json": "{...}", "ts": "..."}

Compatibility
-------------
Implements the ``SessionStoreProtocol`` from ``src/core/interfaces.py``:
    - ``add_message(session_id, role, content)``
    - ``get_messages(session_id) → List[Dict]``
    - ``fork_session(session_id, new_session_id) → str``
    - ``revert_session(session_id, snapshot_id)``
    - ``save_snapshot(session_id, state_json) → str``
    - ``get_snapshot(session_id, snapshot_id) → Optional[str]``
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path

from src.core.memory.file_lock import locked_file
from src.core.memory.jsonl_sidecar_io import atomic_write_json_with_fallback
from src.core.memory.jsonl_store_helpers import (
    build_fork_destination_path,
    build_snapshot_result_payload,
    build_snapshot_sidecar_payload,
    decode_snapshot_reference,
    parse_session_id_from_jsonl_filename,
)

logger = logging.getLogger(__name__)

# Rotate the JSONL file when it exceeds this size in bytes.
_ROTATION_BYTES: int = 256 * 1024  # 256 KB


def _utc_now() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# JsonlSessionStore
# ---------------------------------------------------------------------------


class JsonlSessionStore:
    """Append-only JSONL-based session store.

    Parameters
    ----------
    workdir:
        Project root directory.  Session files are written to
        ``{workdir}/.codingAgent/sessions/``.
    rotation_bytes:
        Rotate the JSONL file when it grows beyond this size.
        Default: 256 KB (``_ROTATION_BYTES``).
    """

    def __init__(
        self,
        workdir: Optional[str] = None,
        rotation_bytes: int = _ROTATION_BYTES,
    ) -> None:
        self._workdir = Path(workdir) if workdir else Path.cwd()
        # Defer canonical path resolution to first write-time call of
        # _get_sessions_dir(). Avoids creating directories during tests.
        self._agent_context_dir: Optional[Path] = None
        self._sessions_dir: Optional[Path] = None
        self._rotation_bytes = rotation_bytes
        # Per-session locks: session_id → threading.Lock
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        # Lock for serialising decisions.json sidecar writes
        self._decisions_lock = threading.Lock()
        # Schema version for compatibility with older tests/tools
        self._SCHEMA_VERSION = 2
        # Maximum records to read in _read_all_records to prevent OOM
        self._MAX_RECORDS = 10_000

    def _get_sessions_dir(self) -> Path:
        """Resolve and return the sessions directory.

        Always uses the canonical .codingAgent directory. Resolved value is
        cached on the instance so subsequent calls reuse the same path.
        """
        if getattr(self, "_sessions_dir", None) is not None:
            return self._sessions_dir  # type: ignore[return-value]

        try:
            from src.tools.tools_config import agent_context_path

            ac = agent_context_path(self._workdir)
        except Exception:
            ac = self._workdir / ".codingAgent"

        self._agent_context_dir = ac
        self._sessions_dir = ac / "sessions"
        return self._sessions_dir

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session_lock(self, session_id: str) -> threading.Lock:
        """Return (creating if necessary) the per-session write lock."""
        with self._locks_lock:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def _session_files(self, session_id: str) -> List[Path]:
        """Return all JSONL files for *session_id* in chronological order.

        Files are ordered by their rotation index (base file last):
          - ``{session_id}.0.jsonl`` (oldest rotation)
          - ``{session_id}.1.jsonl``
          - …
          - ``{session_id}.jsonl``  (current / active)
        """
        sessions_dir = self._get_sessions_dir()
        base = sessions_dir / f"{session_id}.jsonl"
        rotated: List[Tuple[int, Path]] = []
        if sessions_dir.exists():
            for p in sessions_dir.glob(f"{session_id}.*.jsonl"):
                # Extract rotation index from the stem: session_id.N.jsonl
                stem = p.name[len(session_id) + 1 : -len(".jsonl")]
                try:
                    rotated.append((int(stem), p))
                except ValueError:
                    pass
        rotated.sort(key=lambda t: t[0])
        files: List[Path] = [p for _, p in rotated]
        if base.exists():
            files.append(base)
        return files

    def _active_file(self, session_id: str) -> Path:
        """Return the path of the current (writable) JSONL file."""
        sessions_dir = self._get_sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir / f"{session_id}.jsonl"

    def _rotate_if_needed(self, session_id: str) -> None:
        """If the active file exceeds ``_rotation_bytes``, rotate it.

        Rotation renames the current file to ``{session_id}.N.jsonl`` where N
        is the next available index, and lets ``_active_file`` create a fresh
        file on the next append.

        Must be called *with* the session lock held.
        """
        active = self._active_file(session_id)
        if not active.exists():
            return
        if active.stat().st_size < self._rotation_bytes:
            return
        # Find next rotation index
        n = 0
        sessions_dir = self._get_sessions_dir()
        while (sessions_dir / f"{session_id}.{n}.jsonl").exists():
            n += 1
        dest = sessions_dir / f"{session_id}.{n}.jsonl"
        try:
            # Acquire an exclusive lock on the active file while renaming to
            # avoid races with concurrent writers from other processes.
            try:
                with locked_file(active, mode="a"):
                    shutil.move(str(active), str(dest))
            except Exception:
                # Fallback to best-effort move if locking fails.
                shutil.move(str(active), str(dest))
            logger.debug("JsonlSessionStore: rotated %s → %s", active.name, dest.name)
        except Exception as exc:
            logger.warning(
                "JsonlSessionStore: rotation failed for %s: %s", session_id, exc
            )

    def _append(self, session_id: str, record: Dict[str, Any]) -> None:
        """Append *record* as a single JSON line to the session file.

        Acquires the per-session lock, rotates if needed, and writes atomically
        (a single ``write()`` call on a file opened in append mode).
        """
        with self._session_lock(session_id):
            self._rotate_if_needed(session_id)
            active = self._active_file(session_id)
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            try:
                # Use OS-level file locking to coordinate across processes.
                with locked_file(active, mode="a") as f:
                    f.write(line)
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        # Best-effort; don't fail the write if fsync is not available
                        pass
            except Exception as exc:
                logger.error(
                    "JsonlSessionStore: failed to append to %s: %s",
                    active,
                    exc,
                )

    def _read_all_records(self, session_id: str) -> List[Dict[str, Any]]:
        """Read and parse all records across all rotated files for *session_id*.
        
        Limits reading to _MAX_RECORDS to prevent OOM issues.
        """
        records: List[Dict[str, Any]] = []
        records_read = 0
        
        for fpath in self._session_files(session_id):
            if records_read >= self._MAX_RECORDS:
                logger.warning(
                    "JsonlSessionStore: reached max records (%d) for session %s, stopping read",
                    self._MAX_RECORDS, session_id
                )
                break
                
            try:
                # Use a shared/read lock when reading to avoid races with
                # concurrent writers in other processes.
                try:
                    with locked_file(fpath, mode="r") as f:
                        for raw_line in f:
                            if records_read >= self._MAX_RECORDS:
                                break
                            line = raw_line.strip()
                            if not line:
                                continue
                            try:
                                records.append(json.loads(line))
                                records_read += 1
                            except json.JSONDecodeError:
                                logger.debug(
                                    "JsonlSessionStore: skipping malformed line in %s",
                                    fpath.name,
                                )
                except Exception:
                    # Fallback to best-effort direct read when locking fails or
                    # the platform doesn't support it.
                    with fpath.open("r", encoding="utf-8", errors="replace") as f:
                        for raw_line in f:
                            if records_read >= self._MAX_RECORDS:
                                break
                            line = raw_line.strip()
                            if not line:
                                continue
                            try:
                                records.append(json.loads(line))
                                records_read += 1
                            except json.JSONDecodeError:
                                logger.debug(
                                    "JsonlSessionStore: skipping malformed line in %s",
                                    fpath.name,
                                )
            except Exception as exc:
                logger.warning("JsonlSessionStore: could not read %s: %s", fpath, exc)
        
        if records_read >= self._MAX_RECORDS:
            logger.warning(
                "JsonlSessionStore: session %s truncated to %d records (max: %d)",
                session_id, records_read, self._MAX_RECORDS
            )
        
        return records

    # ------------------------------------------------------------------
    # SessionStoreProtocol — core message API
    # ------------------------------------------------------------------

    def add_message(self, session_id: str, role: str, content: str) -> None:
        """Append a conversation message for *session_id*."""
        self._append(
            session_id,
            {
                "type": "message",
                "role": role,
                "content": content,
                "ts": _utc_now(),
            },
        )

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Return all messages for *session_id* in insertion order."""
        return [
            {"role": r["role"], "content": r["content"]}
            for r in self._read_all_records(session_id)
            if r.get("type") == "message"
        ]

    # ------------------------------------------------------------------
    # SessionStoreProtocol — fork and revert
    # ------------------------------------------------------------------

    def fork_session(self, session_id: str, new_session_id: str) -> str:
        """Create an independent copy of *session_id* as *new_session_id*.

        All JSONL files (including rotated ones) are copied byte-for-byte.
        Returns *new_session_id*.
        """
        with self._session_lock(session_id):
            sessions_dir = self._get_sessions_dir()
            sessions_dir.mkdir(parents=True, exist_ok=True)
            src_files = self._session_files(session_id)
            if not src_files:
                raise ValueError(
                    f"fork_session: source session '{session_id}' does not exist"
                )
            for src in src_files:
                # Preserve rotation index in destination name.
                sessions_dir = self._get_sessions_dir()
                dest = build_fork_destination_path(
                    source_session_id=session_id,
                    new_session_id=new_session_id,
                    source_name=src.name,
                    sessions_dir=sessions_dir,
                )
                try:
                    shutil.copy2(str(src), str(dest))
                    logger.debug(
                        "JsonlSessionStore: forked %s → %s", src.name, dest.name
                    )
                except Exception as exc:
                    logger.error(
                        "JsonlSessionStore: fork_session copy failed %s → %s: %s",
                        src,
                        dest,
                        exc,
                    )
        return new_session_id

    def revert_session(self, session_id: str, snapshot_id: str) -> None:
        """Roll back *session_id* to the state captured in *snapshot_id*.

        Reads the snapshot record from the JSONL file to find the
        (file_path, byte_offset) recorded at snapshot time, then truncates the
        active file to that offset — effectively discarding all appends since
        then.

        Rotated files older than the snapshot are kept as-is; they are
        chronologically prior to the snapshot point and are always valid.
        If the snapshot references a rotated file that has since been replaced,
        the revert silently becomes a no-op (the data is already gone).
        """
        snap = self.get_snapshot(session_id, snapshot_id)
        if snap is None:
            logger.warning(
                "JsonlSessionStore.revert_session: snapshot '%s' not found for '%s'",
                snapshot_id,
                session_id,
            )
            return

        try:
            target_file, target_offset = decode_snapshot_reference(snap)
        except Exception as exc:
            logger.warning(
                "JsonlSessionStore.revert_session: bad snapshot metadata: %s", exc
            )
            return

        if not target_file.exists():
            logger.debug(
                "JsonlSessionStore.revert_session: target file %s gone, no-op",
                target_file,
            )
            return

        with self._session_lock(session_id):
            try:
                with target_file.open("r+b") as f:
                    f.truncate(target_offset)
                # If the active file has been rotated since the snapshot,
                # truncating the old (rotated) file is not enough — the
                # current active file also needs to be reset so subsequent
                # appends don't mix old + new data.
                active = self._active_file(session_id)
                if active and active.resolve() != target_file.resolve():
                    logger.info(
                        "JsonlSessionStore: file rotated since snapshot; "
                        "clearing active file %s",
                        active,
                    )
                    with active.open("w") as f:
                        f.truncate(0)
                logger.info(
                    "JsonlSessionStore: reverted '%s' to offset %d in %s",
                    session_id,
                    target_offset,
                    target_file.name,
                )
            except Exception as exc:
                logger.error(
                    "JsonlSessionStore.revert_session: truncate failed: %s", exc
                )

    # ------------------------------------------------------------------
    # SessionStoreProtocol — snapshots
    # ------------------------------------------------------------------

    def _snapshot_dir(self) -> Path:
        """Directory where per-snapshot JSON files are stored."""
        sessions_dir = self._get_sessions_dir()
        return sessions_dir / "snapshots"

    def _snapshot_path(self, session_id: str, snapshot_id: str) -> Path:
        """Path to the sidecar file for a specific snapshot."""
        return self._snapshot_dir() / f"{session_id}.{snapshot_id}.json"

    def save_snapshot(self, session_id: str, state_json: str) -> Optional[str]:
        """Persist a snapshot of *state_json* and return the snapshot ID, or
        ``None`` if the sidecar file could not be written.

        Snapshots are stored in a separate sidecar file
        ``{sessions_dir}/snapshots/{session_id}.{snapshot_id}.json``.
        This file is never modified by ``revert_session``, so snapshots
        survive truncation of the main JSONL file.

        The sidecar records the active JSONL file path and byte offset at the
        moment the snapshot is taken.  ``revert_session`` truncates the JSONL
        to that offset.

        Returns the snapshot ID (a random UUID4 hex string), or ``None`` on
        persistence failure.
        """
        snapshot_id = uuid.uuid4().hex

        with self._session_lock(session_id):
            self._rotate_if_needed(session_id)
            active = self._active_file(session_id)
            # Record the current end-of-file as the revert target.
            offset = active.stat().st_size if active.exists() else 0

        # Write sidecar outside the session lock — independent file.
        snap_dir = self._snapshot_dir()
        snap_dir.mkdir(parents=True, exist_ok=True)
        sidecar = self._snapshot_path(session_id, snapshot_id)
        payload = build_snapshot_sidecar_payload(
            snapshot_id=snapshot_id,
            session_id=session_id,
            state_json=state_json,
            active_file=active,
            offset=offset,
            timestamp=_utc_now(),
        )
        # Write sidecar atomically using the shared fallback-aware helper so a
        # primary atomic writer failure does not silently produce an unusable
        # snapshot id.
        ok = atomic_write_json_with_fallback(sidecar, payload, logger=logger)
        if not ok:
            logger.error(
                "JsonlSessionStore.save_snapshot: sidecar write failed after fallback"
            )
            return None

        return snapshot_id

    def get_snapshot(self, session_id: str, snapshot_id: str) -> Optional[str]:
        """Return the state JSON for *snapshot_id*, or None if not found.

        Reads the sidecar JSON file.  Returns a JSON-encoded envelope with
        ``state_json``, ``_file``, and ``_offset`` fields so
        ``revert_session`` can truncate precisely.
        """
        sidecar = self._snapshot_path(session_id, snapshot_id)
        if not sidecar.exists():
            return None
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return build_snapshot_result_payload(data)
        except Exception as exc:
            logger.warning(
                "JsonlSessionStore.get_snapshot: failed to read sidecar: %s", exc
            )
            return None

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def session_exists(self, session_id: str) -> bool:
        """Return True when at least one JSONL file exists for *session_id*."""
        return bool(self._session_files(session_id))

    def delete_session(self, session_id: str) -> int:
        """Delete all JSONL files for *session_id*.  Returns files deleted."""
        count = 0
        with self._session_lock(session_id):
            for p in self._session_files(session_id):
                try:
                    p.unlink(missing_ok=True)
                    count += 1
                except Exception as exc:
                    logger.warning("JsonlSessionStore: failed to delete %s: %s", p, exc)
        return count

    def list_sessions(self) -> List[str]:
        """Return unique session IDs found in the sessions directory."""
        sessions_dir = self._get_sessions_dir()
        if not sessions_dir.exists():
            return []
        seen: set[str] = set()
        for p in sessions_dir.glob("*.jsonl"):
            sid = parse_session_id_from_jsonl_filename(p.name)
            seen.add(sid)
        return sorted(seen)

    def close(self) -> None:
        """Best-effort close/cleanup for compatibility with SessionStore API.

        JsonlSessionStore opens files per-operation (append/read) and therefore
        has no persistent file descriptors to close. We clear the per-session
        locks map to release Lock instances and keep shutdown tidy.
        """
        try:
            with self._locks_lock:
                self._locks.clear()
        except Exception:
            import traceback

            logger.debug(
                "JsonlSessionStore.close: unexpected error during close\n%s",
                traceback.format_exc(),
            )

    # ------------------------------------------------------------------
    # Compatibility & extended API (sqlite-like surface)
    # ------------------------------------------------------------------

    def get_schema_version(self) -> int:
        """Return the declared schema version for compatibility tests."""
        try:
            return int(self._SCHEMA_VERSION)
        except Exception:
            return 1

    def _write_with_retry(
        self,
        conn: Any,
        sql: str,
        params: tuple = (),
        session_id: Optional[str] = None,
        attempts: int = 5,
        base_backoff: float = 0.05,
    ) -> bool:
        """Best-effort compatibility shim used by tests."""

        def write_operation():
            if hasattr(conn, "execute"):
                conn.execute(sql, params)
            if hasattr(conn, "commit"):
                conn.commit()
            return True

        # Store attempts on self so _write_diagnostic_on_failure can access it
        self._write_retry_attempts = attempts

        # Use shared utility for retry logic
        from src.core.memory._write_retry_utils import write_with_retry

        try:
            return write_with_retry(
                write_func=write_operation,
                max_attempts=attempts,
                base_delay=base_backoff,
                max_delay=1.0,
                context_msg="JsonlSessionStore",
            )
        except Exception as e:
            # Write diagnostic on failure (preserving original behavior)
            self._write_diagnostic_on_failure(session_id or "", e)
            return False

    def _write_diagnostic_on_failure(self, session_id: str, exc: Exception) -> None:
        try:
            # Write directly to .codingAgent directory (not sessions subdirectory)
            # to match what SessionStore._write_with_retry does
            import time

            ts = int(time.time() * 1000)
            sid = session_id or "unknown"

            # Get the agent context directory directly
            try:
                from src.tools.tools_config import agent_context_path

                diag_dir = agent_context_path(self._workdir)
            except Exception:
                diag_dir = self._workdir / ".codingAgent"

            diag_dir.mkdir(parents=True, exist_ok=True)
            diag_path = diag_dir / f"session_store_write_failure_{ts}_{sid}.json"
            payload = {
                "session_id": sid,
                "attempts": getattr(self, "_write_retry_attempts", 5),
                "last_error": str(exc),
                "ts": ts,
            }
            ok = atomic_write_json_with_fallback(diag_path, payload, logger=logger)
            if ok:
                logger.debug(
                    "JsonlSessionStore._write_with_retry: diagnostic atomic write succeeded: %s",
                    diag_path,
                )
        except Exception:
            pass

    # Tool calls, plans, errors, decisions — simple append/read methods

    def add_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: Any,
        result: Any,
        success: bool = True,
    ) -> None:
        self._append(
            session_id,
            {
                "type": "tool_call",
                "tool_name": tool_name,
                "args": args,
                "result": result,
                "success": bool(success),
                "ts": _utc_now(),
            },
        )

    def get_tool_calls(self, session_id: str) -> List[Dict[str, Any]]:
        return [
            r
            for r in self._read_all_records(session_id)
            if r.get("type") == "tool_call"
        ]

    def add_plan(self, session_id: str, plan: Any, status: str = "active") -> None:
        self._append(
            session_id,
            {
                "type": "plan",
                "plan": plan,
                "status": status,
                "ts": _utc_now(),
            },
        )

    def get_plans(self, session_id: str) -> List[Dict[str, Any]]:
        return [
            r for r in self._read_all_records(session_id) if r.get("type") == "plan"
        ]

    def add_error(
        self,
        session_id: str,
        error_type: str,
        error_message: str,
        context: Optional[str] = None,
    ) -> None:
        self._append(
            session_id,
            {
                "type": "error",
                "error_type": error_type,
                "error_message": error_message,
                "context": context,
                "ts": _utc_now(),
            },
        )

    def get_errors(self, session_id: str) -> List[Dict[str, Any]]:
        return [
            r for r in self._read_all_records(session_id) if r.get("type") == "error"
        ]

    def add_decision(
        self, session_id: str, decision: Any, rationale: Optional[str] = None
    ) -> None:
        self._append(
            session_id,
            {
                "type": "decision",
                "decision": decision,
                "rationale": rationale,
                "ts": _utc_now(),
            },
        )
        # Auto-flush recent decisions to a cross-session sidecar for the
        # perception node / decision-memory feature. This is best-effort and
        # protected by a dedicated lock so concurrent writers don't corrupt the
        # decisions.json file.
        try:
            self.write_decisions_json()
        except Exception:
            # Fail silently — add_decision should not raise due to decision
            # memory sidecar write failures.
            import traceback

            logger.debug(
                "JsonlSessionStore: write_decisions_json failed\n%s",
                traceback.format_exc(),
            )

    def get_decisions(self, session_id: str) -> List[Dict[str, Any]]:
        return [
            r for r in self._read_all_records(session_id) if r.get("type") == "decision"
        ]

    # ------------------------------------------------------------------
    # Cross-session decision memory (decisions.json sidecar)
    # ------------------------------------------------------------------

    def _decisions_path(self) -> Path:
        d = Path(
            getattr(self, "_agent_context_dir", None) or self._workdir / ".codingAgent"
        )
        return d / "decisions.json"

    def write_decisions_json(self, limit: int = 50) -> None:
        """Collect recent decisions across all sessions and atomically write
        them to ``{workdir}/.codingAgent/decisions.json``.

        The most recent decisions are selected by timestamp and the result is a
        list of decision objects with keys: session_id, decision, rationale, ts.
        """
        # Gather all decisions from all sessions
        all_decisions: List[Dict[str, Any]] = []
        for sid in self.list_sessions():
            for d in self.get_decisions(sid):
                item = {
                    "session_id": sid,
                    "decision": d.get("decision"),
                    "rationale": d.get("rationale"),
                    "ts": d.get("ts"),
                }
                all_decisions.append(item)

        # Sort by timestamp descending (most recent first). Timestamps are
        # ISO-8601 strings so they sort lexicographically.
        all_decisions.sort(key=lambda x: x.get("ts") or "", reverse=True)
        trimmed = all_decisions[: max(0, int(limit))]

        # For writes prefer the canonical agent-context resolver (may create dirs)
        try:
            from src.tools.tools_config import agent_context_path

            ac_dir = agent_context_path(self._workdir)
        except Exception:
            ac_dir = (
                getattr(self, "_agent_context_dir", None)
                or self._workdir / ".codingAgent"
            )

        path = Path(ac_dir) / "decisions.json"
        with self._decisions_lock:
            ok = atomic_write_json_with_fallback(path, trimmed, logger=logger)
            if not ok:
                logger.error(
                    "JsonlSessionStore.write_decisions_json: fallback write failed"
                )

    def read_recent_decisions(self, max_entries: int = 10) -> List[Dict[str, Any]]:
        """Read and return the recent decisions from the decisions.json
        sidecar. Returns an empty list if the file is missing or malformed.
        """
        path = self._decisions_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            # Return up to max_entries most recent items
            return data[: max(0, int(max_entries))]
        except Exception:
            return []

    # Session state persistence (inline + sidecar)

    def _state_sidecar_path(self, session_id: str) -> Path:
        sessions_dir = self._get_sessions_dir()
        d = sessions_dir / "state"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{session_id}.json"

    def save_session_state(
        self,
        session_id: str,
        state: dict,
        role: Optional[str] = None,
        task: Optional[str] = None,
    ) -> None:
        # Append inline for full history
        self._append(
            session_id,
            {
                "type": "session_state",
                "state": state,
                "role": role,
                "task": task,
                "ts": _utc_now(),
            },
        )

        # Also write a durable sidecar atomically. Prefer the shared helper
        # but fall back to the legacy mkstemp+replace approach on error.
        side = self._state_sidecar_path(session_id)
        payload = {"state": state, "role": role, "task": task, "ts": _utc_now()}
        atomic_write_json_with_fallback(side, payload, logger=logger)

    def load_session_state(self, session_id: str) -> Optional[dict]:
        # Prefer the sidecar when present
        side = self._state_sidecar_path(session_id)
        if side.exists():
            try:
                data = json.loads(side.read_text(encoding="utf-8"))
                return data.get("state")
            except Exception:
                pass

        # Fallback: scan for the most recent session_state record
        records = self._read_all_records(session_id)
        for r in reversed(records):
            if r.get("type") == "session_state":
                return r.get("state")
        return None

    # Child session registration

    def register_child_session(
        self,
        parent_session_id: str,
        child_session_id: str,
        role: Optional[str] = None,
        task: Optional[str] = None,
    ) -> None:
        self._append(
            parent_session_id or "unknown",
            {
                "type": "session_child",
                "parent_session_id": parent_session_id,
                "child_session_id": child_session_id,
                "role": role,
                "task": task,
                "ts": _utc_now(),
            },
        )

    def get_child_sessions(self, parent_session_id: str) -> List[Dict[str, Any]]:
        return [
            r
            for r in self._read_all_records(parent_session_id)
            if r.get("type") == "session_child"
            and r.get("parent_session_id") == parent_session_id
        ]

    def get_session_tree(self, session_id: str) -> Dict[str, Any]:
        # Build parent->children map by scanning all sessions
        map_parent: Dict[str, List[Dict[str, Any]]] = {}
        for sid in self.list_sessions():
            for r in self._read_all_records(sid):
                if r.get("type") == "session_child":
                    p = r.get("parent_session_id")
                    key = str(p) if p is not None else ""
                    map_parent.setdefault(key, []).append(r)

        def _build(sid: str, _visited: set | None = None) -> Dict[str, Any]:
            if _visited is None:
                _visited = set()
            if sid in _visited:
                logger.warning("get_session_tree: cycle detected at session_id=%r, stopping recursion", sid)
                return {"session_id": sid, "children": [], "cycle": True}
            _visited = _visited | {sid}
            children = []
            for ch in map_parent.get(str(sid), []):
                child_id = ch.get("child_session_id")
                if child_id is None:
                    continue
                children.append(_build(child_id, _visited))
            return {"session_id": sid, "children": children}

        return _build(session_id)

    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        records = self._read_all_records(session_id)
        summary: Dict[str, Any] = {
            "session_id": session_id,
            "messages": 0,
            "message_count": 0,
            # Keep both legacy plural keys and the explicit *_count names tests
            # expect. This preserves compatibility for callers that used either.
            "tool_calls": 0,
            "tool_call_count": 0,
            "errors": 0,
            "error_count": 0,
            "plans": 0,
            "decisions": 0,
        }
        for r in records:
            t = r.get("type")
            if t == "message":
                summary["messages"] += 1
                summary["message_count"] += 1
            elif t == "tool_call":
                summary["tool_calls"] += 1
                summary["tool_call_count"] += 1
            elif t == "error":
                summary["errors"] += 1
                summary["error_count"] += 1
            elif t == "plan":
                summary["plans"] += 1
            elif t == "decision":
                summary["decisions"] += 1
        return summary
