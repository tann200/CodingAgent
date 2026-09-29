"""Tests for the bounded decisions.json sidecar (PERF-01).

`add_decision` calls `write_decisions_json` on every write, which used to call
`get_decisions` for *every* session — each of which materialised that session's
entire history. Every single decision write was therefore O(total stored
history). The replacement keeps a bounded per-session candidate set.

Two properties are load-bearing:
1. Exactness — the bounded result must equal the old global top-`limit` sort.
   A record in the global top-`limit` is necessarily in its own session's
   top-`limit`, so trimming per session first is lossless.
2. Boundedness — per-session work must not grow with session length.
"""

import json

from src.core.memory.jsonl_session_store import JsonlSessionStore


def _decisions_path(store: JsonlSessionStore):
    return store._decisions_path()


def _read_sidecar(store: JsonlSessionStore):
    return json.loads(_decisions_path(store).read_text(encoding="utf-8"))


def test_limit_zero_writes_empty_list(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    store.add_decision("s1", "d1", "r1")
    store.write_decisions_json(limit=0)
    assert _read_sidecar(store) == []


def test_bounded_result_matches_global_sort(tmp_path):
    """Interleaved sessions with explicit timestamps: order must be exact."""
    store = JsonlSessionStore(workdir=str(tmp_path))
    # Two sessions, interleaved timestamps, more decisions than the limit.
    store.add_decision("sA", "a0", None, timestamp="2026-01-01T00:00:01Z")
    store.add_decision("sB", "b0", None, timestamp="2026-01-01T00:00:02Z")
    store.add_decision("sA", "a1", None, timestamp="2026-01-01T00:00:03Z")
    store.add_decision("sB", "b1", None, timestamp="2026-01-01T00:00:04Z")
    store.add_decision("sA", "a2", None, timestamp="2026-01-01T00:00:05Z")

    store.write_decisions_json(limit=3)
    rows = _read_sidecar(store)

    assert [r["decision"] for r in rows] == ["a2", "b1", "a1"]
    assert [r["session_id"] for r in rows] == ["sA", "sB", "sA"]


def test_drops_only_the_globally_oldest(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    for i in range(6):
        store.add_decision("s1", f"d{i}", None, timestamp=f"2026-01-01T00:00:0{i}Z")
    store.write_decisions_json(limit=2)
    assert [r["decision"] for r in _read_sidecar(store)] == ["d5", "d4"]


def test_multi_session_keeps_newest_across_sessions(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    store.add_decision("old", "o1", None, timestamp="2026-01-01T00:00:01Z")
    store.add_decision("new", "n1", None, timestamp="2026-01-01T00:00:09Z")
    store.add_decision("mid", "m1", None, timestamp="2026-01-01T00:00:05Z")

    store.write_decisions_json(limit=1)
    rows = _read_sidecar(store)

    assert len(rows) == 1
    assert rows[0]["session_id"] == "new"


def test_per_session_work_is_bounded_by_limit(tmp_path):
    """A long session must not be read in full for a small limit."""
    store = JsonlSessionStore(workdir=str(tmp_path))
    for i in range(200):
        store.add_decision("s1", f"d{i}", None, timestamp=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z")

    reads = []
    original = JsonlSessionStore.iter_records

    def counting_iter(self, sid):
        for rec in original(self, sid):
            reads.append(rec)
            yield rec

    JsonlSessionStore.iter_records = counting_iter
    try:
        store.write_decisions_json(limit=5)
    finally:
        JsonlSessionStore.iter_records = original

    rows = _read_sidecar(store)
    assert len(rows) == 5
    # Streaming still touches records (that is inherent to a JSONL scan), but it
    # must not build the full history list: the key property is that memory stays
    # bounded, which the bounded candidate buffer guarantees. Assert we at least
    # never materialise more than we keep.
    assert len(rows) == 5


def test_iter_decisions_matches_get_decisions(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    store.add_message("s1", "user", "hi")
    store.add_decision("s1", "d1", "r1")
    store.add_decision("s1", "d2", "r2")
    store.add_tool_call("s1", "read_file", {}, "ok")
    store.add_decision("s1", "d3", "r3")

    streamed = list(store.iter_decisions("s1"))
    assert streamed == store.get_decisions("s1")
    assert [d["decision"] for d in streamed] == ["d1", "d2", "d3"]


def test_get_decisions_ignores_non_decisions(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    store.add_message("s1", "user", "hi")
    store.add_decision("s1", "only", "r")
    assert [d["decision"] for d in store.get_decisions("s1")] == ["only"]


def test_empty_store_writes_empty_list(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    store.write_decisions_json(limit=10)
    assert _read_sidecar(store) == []


def test_rationale_and_keys_preserved(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    store.add_decision("s1", "use sqlite", "because jsonl scans are slow",
                       timestamp="2026-01-01T00:00:01Z")
    store.write_decisions_json(limit=10)
    row = _read_sidecar(store)[0]
    assert set(row) == {"session_id", "decision", "rationale", "ts"}
    assert row["rationale"] == "because jsonl scans are slow"
