"""
session_store.py — JSON-based conversation snapshot persistence.

Thin port of the claw-code-main session_store pattern for headless/CLI use.
The TUI's ``_save_session_snapshot()`` in ``tui/src/ui/app.py`` writes these
files; ``SessionListScreen`` reads them via ``list_sessions()`` / ``load_session()``.

Write path: ``get_sessions_dir()/session_{session_id}.json`` (use
``src.core.paths.get_sessions_dir()`` to locate user session snapshots).
(Windows: %LOCALAPPDATA%/CodingAgent/sessions/)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.core.paths import get_sessions_dir

_SESSIONS_DIR = get_sessions_dir()
_SCHEMA_VERSION = 1


@dataclass
class StoredSession:
    """Schema for a persisted conversation snapshot (version 1)."""

    version: int
    session_id: str
    task_name: str
    working_dir: str
    messages: list[dict]
    message_count: int
    turn_count: int
    input_tokens: int
    output_tokens: int
    created_at: str  # ISO-8601 UTC timestamp string
    timestamp: float = field(default_factory=time.time)  # unix float (legacy compat)


def _sessions_dir() -> Path:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return _SESSIONS_DIR


def save_session(session: StoredSession, path: Path | None = None) -> Path:
    """Atomically write *session* to disk.  Returns the path written."""
    if path is None:
        path = _sessions_dir() / f"session_{session.session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(session)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_session(session_id: str) -> StoredSession | None:
    """Load a single session by its UUID.  Returns *None* if not found."""
    candidates = [
        _sessions_dir() / f"session_{session_id}.json",
    ]
    # Also search glob in case the file was written with a timestamp name
    for p in _sessions_dir().glob(f"session_*{session_id}*.json"):
        if p not in candidates:
            candidates.append(p)
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return _from_dict(data)
            except Exception:
                pass
    return None


def list_sessions(limit: int = 50) -> list[StoredSession]:
    """Return up to *limit* sessions, most recently modified first."""
    sd = _sessions_dir()
    sessions: list[StoredSession] = []
    paths = sorted(
        sd.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for p in paths[: limit * 2]:  # read extra to account for parse failures
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            sessions.append(_from_dict(data))
        except Exception:
            continue
        if len(sessions) >= limit:
            break
    return sessions


def make_session_id() -> str:
    """Generate a new stable UUID for a session."""
    return str(uuid.uuid4())


# ── Internal helpers ──────────────────────────────────────────────────────────


def _from_dict(data: dict) -> StoredSession:
    """Deserialise a raw dict, tolerating missing fields from older schema versions."""
    return StoredSession(
        version=int(data.get("version", _SCHEMA_VERSION)),
        session_id=str(data.get("session_id") or make_session_id()),
        task_name=str(data.get("task_name") or data.get("task") or ""),
        working_dir=str(data.get("working_dir") or ""),
        messages=list(data.get("messages") or []),
        message_count=int(data.get("message_count") or len(data.get("messages") or [])),
        turn_count=int(data.get("turn_count") or 0),
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        created_at=str(data.get("created_at") or data.get("timestamp") or ""),
        timestamp=float(data.get("timestamp") or time.time()),
    )
