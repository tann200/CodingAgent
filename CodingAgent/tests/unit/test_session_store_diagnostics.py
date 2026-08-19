# -*- coding: utf-8 -*-
import json
import sqlite3
from pathlib import Path


from src.core.memory.session_store import SessionStore


class FakeConnAlwaysLocked:
    """Fake connection that always raises SQLITE_BUSY on execute/commit."""

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    def commit(self):
        raise sqlite3.OperationalError("database is locked")


def test_write_with_retry_exhausts_and_writes_diagnostic(tmp_path: Path):
    store = SessionStore(workdir=str(tmp_path))

    conn = FakeConnAlwaysLocked()

    ok = store._write_with_retry(
        conn,
        "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
        ("s1", "user", "hi"),
        session_id="s1",
        attempts=2,
        base_backoff=0.001,
    )
    assert ok is False

    diag_dir = tmp_path / ".codingAgent"
    assert diag_dir.exists()

    # Find the diagnostic file
    files = list(diag_dir.glob("session_store_write_failure_*.json"))
    assert len(files) >= 1

    data = json.loads(files[-1].read_text(encoding="utf-8"))
    assert data.get("db_path") is not None
    assert data.get("session_id") == "s1"
    assert data.get("attempts") == 2
    assert (
        "locked" in data.get("last_error", "").lower()
        or "busy" in data.get("last_error", "").lower()
    )


class FakeCursor:
    def fetchall(self):
        return []


class FakeConnTransient:
    """Fake connection that raises SQLITE_BUSY a few times then succeeds."""

    def __init__(self, fail_times: int = 2):
        self._fails = fail_times

    def execute(self, *args, **kwargs):
        if self._fails > 0:
            self._fails -= 1
            raise sqlite3.OperationalError("database is locked")
        return FakeCursor()

    def commit(self):
        if self._fails > 0:
            raise sqlite3.OperationalError("database is locked")
        return None


def test_write_with_retry_succeeds_after_retries(tmp_path: Path):
    store = SessionStore(workdir=str(tmp_path))
    # fake connection will fail twice then succeed
    conn = FakeConnTransient(fail_times=2)

    ok = store._write_with_retry(
        conn,
        "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
        ("s2", "user", "hi"),
        session_id="s2",
        attempts=5,
        base_backoff=0.001,
    )
    assert ok is True

    # Ensure no diagnostic file for this session exists
    diag_dir = tmp_path / ".codingAgent"
    files = list(diag_dir.glob("session_store_write_failure_*_s2.json"))
    assert len(files) == 0
