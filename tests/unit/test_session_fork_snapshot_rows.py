from __future__ import annotations

from pathlib import Path

from src.core.memory.sqlite_session_store import SqliteSessionStore as SessionStore


def test_fork_copies_snapshot_rows(tmp_path: Path) -> None:
    """When a session with snapshots is forked the fork retains access to
    the snapshot row-level data so revert_session(snapshot_id) works on the
    fork as well.
    """
    store = SessionStore(workdir=str(tmp_path))

    # Create a session and take a snapshot
    sid = "src"
    store.add_message(sid, "user", "before")
    snap_id = store.save_snapshot(sid, '{"task": "t1"}')

    # Mutate the session then fork
    store.add_message(sid, "assistant", "after")
    fork = store.fork_session(sid)

    # Reverting the fork to the snapshot_id should restore the fork's rows
    res = store.revert_session(fork, snap_id)
    assert res["ok"] is True
    msgs = store.get_messages(fork)
    # After revert the fork should have only the message present at snapshot
    assert len(msgs) == 1
    assert msgs[0]["content"] == "before"
