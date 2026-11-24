from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.core.memory.sqlite_session_store import SqliteSessionStore


class BadConn:
    def __init__(self, exc: Exception):
        self._exc = exc

    def execute(self, *args, **kwargs):
        raise self._exc

    def commit(self):
        return None


def _find_diag_file(diag_dir: Path):
    pals = list(diag_dir.glob("session_store_write_failure_*.json"))
    return pals[-1] if pals else None


def test_sqlite_write_with_retry_writes_diag(tmp_path: Path) -> None:
    store = SqliteSessionStore(workdir=str(tmp_path))

    bad = BadConn(sqlite3.OperationalError("database is locked"))

    ok = store._write_with_retry(
        bad, "INSERT INTO x VALUES (?)", (1,), session_id="s2", attempts=1
    )
    assert ok is False

    diag_dir = Path(tmp_path) / ".codingAgent"
    diag = _find_diag_file(diag_dir)
    assert diag is not None
    payload = json.loads(diag.read_text(encoding="utf-8"))
    assert payload.get("session_id") == "s2"
    assert ("SQLITE_BUSY" in payload.get("last_error", "")) or (
        "LOCKED" in payload.get("last_error", "")
    )
