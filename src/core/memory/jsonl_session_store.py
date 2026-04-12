"""jsonl_session_store.py — Append-only JSONL session store (TASK-7).

A drop-in alternative to the SQLite-backed ``SessionStore`` that satisfies
``SessionStoreProtocol`` (defined in ``src/core/interfaces.py``).

Design goals
------------
- **Append-only**: writes go only to the end of a ``.jsonl`` file; no UPDATE
  or DELETE.  This makes crash-safety trivial — a partial final line is simply
  ignored on read.
- **Single-file per session**: ``{workdir}/.agent-context/sessions/{session_id}.jsonl``
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
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        ``{workdir}/.agent-context/sessions/``.
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
        self._sessions_dir = self._workdir / ".agent-context" / "sessions"
        self._rotation_bytes = rotation_bytes
        # Per-session locks: session_id → threading.Lock
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

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
        base = self._sessions_dir / f"{session_id}.jsonl"
        rotated: List[Tuple[int, Path]] = []
        if self._sessions_dir.exists():
            for p in self._sessions_dir.glob(f"{session_id}.*.jsonl"):
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
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        return self._sessions_dir / f"{session_id}.jsonl"

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
        while (self._sessions_dir / f"{session_id}.{n}.jsonl").exists():
            n += 1
        dest = self._sessions_dir / f"{session_id}.{n}.jsonl"
        try:
            shutil.move(str(active), str(dest))
            logger.debug(
                "JsonlSessionStore: rotated %s → %s", active.name, dest.name
            )
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
                with active.open("a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as exc:
                logger.error(
                    "JsonlSessionStore: failed to append to %s: %s",
                    active,
                    exc,
                )

    def _read_all_records(self, session_id: str) -> List[Dict[str, Any]]:
        """Read and parse all records across all rotated files for *session_id*."""
        records: List[Dict[str, Any]] = []
        for fpath in self._session_files(session_id):
            try:
                with fpath.open("r", encoding="utf-8", errors="replace") as f:
                    for raw_line in f:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            logger.debug(
                                "JsonlSessionStore: skipping malformed line in %s",
                                fpath.name,
                            )
            except Exception as exc:
                logger.warning(
                    "JsonlSessionStore: could not read %s: %s", fpath, exc
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
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            src_files = self._session_files(session_id)
            if not src_files:
                raise ValueError(
                    f"fork_session: source session '{session_id}' does not exist"
                )
            for src in src_files:
                # Preserve rotation index in destination name
                if src.name == f"{session_id}.jsonl":
                    dest = self._sessions_dir / f"{new_session_id}.jsonl"
                else:
                    suffix = src.name[len(session_id):]  # e.g. ".0.jsonl"
                    dest = self._sessions_dir / f"{new_session_id}{suffix}"
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
            meta = json.loads(snap)
            target_file = Path(meta["_file"])
            target_offset = int(meta["_offset"])
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
        return self._sessions_dir / "snapshots"

    def _snapshot_path(self, session_id: str, snapshot_id: str) -> Path:
        """Path to the sidecar file for a specific snapshot."""
        return self._snapshot_dir() / f"{session_id}.{snapshot_id}.json"

    def save_snapshot(self, session_id: str, state_json: str) -> str:
        """Persist a snapshot of *state_json* and return the snapshot ID.

        Snapshots are stored in a separate sidecar file
        ``{sessions_dir}/snapshots/{session_id}.{snapshot_id}.json``.
        This file is never modified by ``revert_session``, so snapshots
        survive truncation of the main JSONL file.

        The sidecar records the active JSONL file path and byte offset at the
        moment the snapshot is taken.  ``revert_session`` truncates the JSONL
        to that offset.

        Returns the snapshot ID (a random UUID4 hex string).
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
        payload = {
            "snapshot_id": snapshot_id,
            "session_id": session_id,
            "state_json": state_json,
            "_file": str(active),
            "_offset": offset,
            "ts": _utc_now(),
        }
        try:
            sidecar.write_text(
                json.dumps(payload, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error(
                "JsonlSessionStore.save_snapshot: sidecar write failed: %s", exc
            )

        return snapshot_id

    def get_snapshot(
        self, session_id: str, snapshot_id: str
    ) -> Optional[str]:
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
            return json.dumps(
                {
                    "state_json": data.get("state_json", ""),
                    "_file": data.get("_file", ""),
                    "_offset": data.get("_offset", 0),
                }
            )
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
                    logger.warning(
                        "JsonlSessionStore: failed to delete %s: %s", p, exc
                    )
        return count

    def list_sessions(self) -> List[str]:
        """Return unique session IDs found in the sessions directory."""
        if not self._sessions_dir.exists():
            return []
        seen: set[str] = set()
        for p in self._sessions_dir.glob("*.jsonl"):
            # Parse session_id from filename: strip trailing .N.jsonl or .jsonl
            name = p.stem  # e.g. "abc123" or "abc123.0"
            if "." in name:
                # Rotated file: "session_id.N"
                sid = name.rsplit(".", 1)[0]
            else:
                sid = name
            seen.add(sid)
        return sorted(seen)
