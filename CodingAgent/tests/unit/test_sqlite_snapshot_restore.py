from __future__ import annotations

import json
from pathlib import Path

from src.core.memory.sqlite_session_store import SqliteSessionStore as SessionStore


def _make_session_rows(store: SessionStore, sid: str) -> dict:
    # Add representative rows across tables and return a summary of the
    # inserted values to validate restores.
    store.add_message(sid, "user", "m1")
    store.add_message(sid, "assistant", "m2")
    store.add_tool_call(sid, "t1", {"a": 1}, {"ok": True}, True)
    store.add_error(sid, "TypeError", "boom", {"ctx": 1})
    store.add_plan(sid, '["step"]', "active")
    store.add_decision(sid, "decide", "because")
    store.register_child_session(sid, "child1", role="assistant", task="subtask")

    summary = {
        "messages": [m["content"] for m in store.get_messages(sid)],
        "tool_calls": [tc["tool_name"] for tc in store.get_tool_calls(sid)],
        "errors": [e["error_type"] for e in store.get_errors(sid)],
        "plans": [p["plan"] for p in store.get_plans(sid)],
        "decisions": [d["decision"] for d in store.get_decisions(sid)],
        "children": [c["child_session_id"] for c in store.get_child_sessions(sid)],
    }
    return summary


def test_save_snapshot_and_revert_restores_all_tables(tmp_path: Path) -> None:
    store = SessionStore(workdir=str(tmp_path))
    sid = "s1"

    before = _make_session_rows(store, sid)
    snap = store.save_snapshot(sid, json.dumps({"meta": "v1"}))

    # Mutate the session: add extra rows to ensure revert removes them and
    # restores the snapshot contents.
    store.add_message(sid, "user", "extra")
    store.add_tool_call(sid, "t2", {"b": 2}, {"ok": False}, False)

    # Now revert to the snapshot (deterministic row-level restore)
    res = store.revert_session(sid, snap)
    assert res["ok"] is True

    after_msgs = [m["content"] for m in store.get_messages(sid)]
    assert after_msgs == before["messages"]

    after_tcs = [tc["tool_name"] for tc in store.get_tool_calls(sid)]
    assert after_tcs == before["tool_calls"]

    after_errs = [e["error_type"] for e in store.get_errors(sid)]
    assert after_errs == before["errors"]

    after_plans = [p["plan"] for p in store.get_plans(sid)]
    assert after_plans == before["plans"]

    after_decs = [d["decision"] for d in store.get_decisions(sid)]
    assert after_decs == before["decisions"]

    after_children = [c["child_session_id"] for c in store.get_child_sessions(sid)]
    assert after_children == before["children"]


def test_fork_session_copies_snapshot_rows_and_fork_can_revert(tmp_path: Path) -> None:
    store = SessionStore(workdir=str(tmp_path))
    sid = "source"

    before = _make_session_rows(store, sid)
    snap = store.save_snapshot(sid, json.dumps({"meta": "v2"}))

    # Fork the session; the fork should have snapshot metadata and
    # row-level snapshot data available so revert on the fork works.
    fk = store.fork_session(sid, fork_id="forked")

    # Mutate the fork, then revert it
    store.add_message(fk, "user", "fork-extra")
    # Ensure the fork now differs
    assert [m["content"] for m in store.get_messages(fk)] != before["messages"]

    res = store.revert_session(fk, snap)
    assert res["ok"] is True

    # After revert, fork should match the original snapshot contents
    assert [m["content"] for m in store.get_messages(fk)] == before["messages"]
    assert [tc["tool_name"] for tc in store.get_tool_calls(fk)] == before["tool_calls"]
    assert [e["error_type"] for e in store.get_errors(fk)] == before["errors"]
    assert [p["plan"] for p in store.get_plans(fk)] == before["plans"]
    assert [d["decision"] for d in store.get_decisions(fk)] == before["decisions"]
    assert [c["child_session_id"] for c in store.get_child_sessions(fk)] == before[
        "children"
    ]
