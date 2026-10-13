"""Unit tests for SQLite session deletion.

Regression coverage for Audit §15 item 6a:
SQLite session deletion must cascade cleanly across all session tables:
- session_snapshot_rows deleted by snapshot_id (not session_id)
- session_snapshots deleted
- session_children deleted by parent_session_id or child_session_id
- messages, tool_calls, errors, plans, decisions, mistakes, session_plans deleted
- Shared snapshots from forked sessions must not be purged when the source is deleted.
"""

from __future__ import annotations

from pathlib import Path

from src.core.memory.sqlite_session_store import SqliteSessionStore


def test_delete_session_removes_all_records(tmp_path: Path) -> None:
    store = SqliteSessionStore(workdir=str(tmp_path))
    sid = "test-session-delete"

    # Add records to multiple tables
    store.add_message(sid, "user", "hello")
    store.add_message(sid, "assistant", "world")
    store.add_tool_call(sid, "read_file", {"path": "a.txt"}, {"content": "abc"}, True)
    store.add_error(sid, "TestError", "An error occurred", "context info")
    store.add_plan(sid, "Step 1\nStep 2", "active")
    store.add_decision(sid, "Use SQLite", "faster and local")
    store.add_mistake(sid, "typo", "Fixed typo in config", "src/config.py")

    # Add snapshot
    snap_id = store.save_snapshot(sid, '{"task": "do test"}')
    assert snap_id is not None

    # Register a child session
    store.register_child_session(sid, "child-session-1", role="coder", task="subtask")

    # Verify data exists before delete
    assert len(store.get_messages(sid)) == 2
    assert len(store.get_tool_calls(sid)) == 1
    assert len(store.get_errors(sid)) == 1
    assert len(store.get_decisions(sid)) == 1
    conn = store._get_connection()
    assert conn.execute("SELECT COUNT(*) as cnt FROM mistakes WHERE session_id=?", (sid,)).fetchone()["cnt"] >= 1
    assert store.get_snapshot(sid, snap_id) is not None
    assert len(store.get_child_sessions(sid)) == 1

    # Perform delete
    deleted_count = store.delete_session(sid)
    assert deleted_count > 0

    # Verify all records for this session are gone
    assert len(store.get_messages(sid)) == 0
    assert len(store.get_tool_calls(sid)) == 0
    assert len(store.get_errors(sid)) == 0
    assert len(store.get_decisions(sid)) == 0
    assert conn.execute("SELECT COUNT(*) as cnt FROM mistakes WHERE session_id=?", (sid,)).fetchone()["cnt"] == 0
    assert store.get_snapshot(sid, snap_id) is None
    assert len(store.get_child_sessions(sid)) == 0

    # Raw check on snapshot_rows
    conn = store._get_connection()
    rows = conn.execute(
        "SELECT COUNT(*) as cnt FROM session_snapshot_rows WHERE snapshot_id=?",
        (snap_id,),
    ).fetchone()
    assert rows["cnt"] == 0


def test_delete_session_preserves_forked_snapshot_rows(tmp_path: Path) -> None:
    store = SqliteSessionStore(workdir=str(tmp_path))
    sid = "parent-session"

    store.add_message(sid, "user", "initial prompt")
    snap_id = store.save_snapshot(sid, '{"task": "snapshot 1"}')

    # Fork session (copies snapshot reference)
    fork_id = store.fork_session(sid)
    assert fork_id is not None

    # Delete parent session
    store.delete_session(sid)

    # Fork should still retain its snapshot and revert capability
    assert store.get_snapshot(fork_id, snap_id) is not None
    conn = store._get_connection()
    rows = conn.execute(
        "SELECT COUNT(*) as cnt FROM session_snapshot_rows WHERE snapshot_id=?",
        (snap_id,),
    ).fetchone()
    assert rows["cnt"] > 0

    # Now deleting fork cleans up snapshot_rows too
    store.delete_session(fork_id)
    rows_after = conn.execute(
        "SELECT COUNT(*) as cnt FROM session_snapshot_rows WHERE snapshot_id=?",
        (snap_id,),
    ).fetchone()
    assert rows_after["cnt"] == 0
