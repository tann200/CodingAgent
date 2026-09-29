"""Regression tests for JsonlSessionStore.get_recent_sessions ordering.

The method feeds ``distiller.retrieve_relevant_prior_sessions``, which picks
which prior sessions are worth putting in front of the model. It previously
sorted session ids alphabetically, so the "most recent" N sessions were in
fact an arbitrary alphabetical slice — surfacing the *oldest* sessions whenever
their names happened to sort first.
"""

import os
import time

from src.core.memory.jsonl_session_store import JsonlSessionStore


def _touch_sessions(store: JsonlSessionStore, session_id: str) -> None:
    store.add_message(session_id, "user", f"hello from {session_id}")


def test_orders_by_recency_not_alphabetically(tmp_path):
    """Alphabetically-first id must not win when it is the oldest session."""
    store = JsonlSessionStore(workdir=str(tmp_path))
    # "a-first" is OLDEST but sorts first alphabetically; "z-last" is NEWEST.
    _touch_sessions(store, "a-first")
    time.sleep(0.02)
    _touch_sessions(store, "z-last")

    recent = store.get_recent_sessions(limit=1)

    assert len(recent) == 1
    assert recent[0]["session_id"] == "z-last", (
        "get_recent_sessions must order by recency; got "
        f"{recent[0]['session_id']!r} (alphabetical order leaked back in)"
    )


def test_full_order_is_newest_first(tmp_path):
    """Created mmm -> aaa -> zzz, so recency order must be the reverse."""
    store = JsonlSessionStore(workdir=str(tmp_path))
    for sid in ("mmm", "aaa", "zzz"):
        _touch_sessions(store, sid)
        time.sleep(0.02)

    ordered = [r["session_id"] for r in store.get_recent_sessions(limit=10)]

    assert ordered == ["zzz", "aaa", "mmm"]


def test_uses_rotated_file_mtime(tmp_path):
    """A session whose newest write is a rotation must outrank an idle one."""
    store = JsonlSessionStore(workdir=str(tmp_path))
    _touch_sessions(store, "quiet")
    time.sleep(0.02)
    _touch_sessions(store, "busy")

    sessions_dir = store._get_sessions_dir()
    rotated = sessions_dir / "quiet.0.jsonl"
    rotated.write_text('{"type": "message"}\n', encoding="utf-8")
    now = time.time()
    os.utime(rotated, (now, now))

    ordered = [r["session_id"] for r in store.get_recent_sessions(limit=10)]

    assert ordered[0] == "quiet", "rotation mtime must count toward recency"


def test_respects_limit_and_supports_zero(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    for sid in ("a", "b", "c"):
        _touch_sessions(store, sid)

    assert len(store.get_recent_sessions(limit=2)) == 2
    assert store.get_recent_sessions(limit=0) == []


def test_returns_summary_key_preserved(tmp_path):
    """The jsonl store returns `summary`; keep that shape (tests depend on it)."""
    store = JsonlSessionStore(workdir=str(tmp_path))
    _touch_sessions(store, "s1")

    entry = store.get_recent_sessions(limit=1)[0]

    assert set(entry) == {"session_id", "summary"}
    assert entry["summary"]["message_count"] == 1


def test_empty_store_returns_empty(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    assert store.get_recent_sessions(limit=5) == []


def test_missing_sessions_dir_returns_empty(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path / "nope"))
    assert store.get_recent_sessions(limit=5) == []
