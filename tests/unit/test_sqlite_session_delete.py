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


def test_sqlite_fts5_rowid_sync_after_delete_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(workdir=str(tmp_path))

    # Session A: insert message 'alpha'
    store.add_message("session_a", "user", "alpha message")
    res_a = store.search("alpha")
    assert len(res_a) == 1
    assert res_a[0]["content"] == "alpha message"

    # Delete Session A
    store.delete_session("session_a")

    # Session B: insert message 'beta'
    store.add_message("session_b", "user", "beta message")

    # Before SPEC-12 fix, messages.id was 2 while messages_fts.rowid was 1 or desynced,
    # causing store.search('beta') to return [] or join the wrong row.
    res_b = store.search("beta")
    assert len(res_b) == 1
    assert res_b[0]["session_id"] == "session_b"
    assert res_b[0]["content"] == "beta message"


def test_sqlite_delete_session_cleans_mistakes_fts_and_refreshes_decisions(tmp_path: Path) -> None:
    store = SqliteSessionStore(workdir=str(tmp_path))
    sid = "sess_mistake_decision"

    store.add_decision(sid, "decision alpha", "rationale alpha")
    store.add_mistake(sid, "mistake summary", "context details", "edit_file")

    conn = store._get_connection()
    fts_mistakes = conn.execute("SELECT COUNT(*) as cnt FROM mistakes_fts WHERE session_id=?", (sid,)).fetchone()["cnt"]
    assert fts_mistakes >= 1

    decisions_path = tmp_path / ".codingAgent" / "decisions.json"
    assert decisions_path.exists()

    # Delete session
    store.delete_session(sid)

    # mistakes_fts must be cleaned up
    fts_mistakes_after = conn.execute("SELECT COUNT(*) as cnt FROM mistakes_fts WHERE session_id=?", (sid,)).fetchone()["cnt"]
    assert fts_mistakes_after == 0

    # decisions.json must have been updated to remove the deleted decision
    decisions = store.read_recent_decisions()
    assert all(d.get("session_id") != sid for d in decisions)


def test_jsonl_session_store_preserves_dotted_session_ids_and_parity(tmp_path: Path) -> None:
    from src.core.memory.jsonl_session_store import JsonlSessionStore

    store = JsonlSessionStore(workdir=str(tmp_path))
    sid = "release-1.2.3"

    store.add_message(sid, "user", "release message")
    store.add_decision(sid, "deploy release", "tag created")
    store.add_mistake(sid, "version mismatch", "forgot to bump patch", "git_tools")

    snap_id = store.save_snapshot(sid, '{"state": "ready"}')
    assert snap_id is not None
    store.save_session_state(sid, {"status": "ok"})

    # Verify session ID is not truncated to 'release-1.2'
    sessions = store.list_sessions()
    assert sid in sessions
    assert "release-1.2" not in sessions

    # Verify search_mistakes works in JsonlSessionStore
    mistakes = store.search_mistakes("mismatch")
    assert len(mistakes) >= 1
    assert "mismatch" in mistakes[0]["summary"]

    # Verify delete_session cleans up files, snapshots, state, and updates decisions
    deleted_count = store.delete_session(sid)
    assert deleted_count >= 1

    assert sid not in store.list_sessions()
    assert store.get_snapshot(sid, snap_id) is None
    assert store.load_session_state(sid) is None
    decisions = store.read_recent_decisions()
    assert all(d.get("session_id") != sid for d in decisions)
