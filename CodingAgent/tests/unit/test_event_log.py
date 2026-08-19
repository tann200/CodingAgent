"""tests/unit/test_event_log.py — Unit tests for S4-B EventLog."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.core.orchestration.event_log import EventKind, EventLog, EventRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def log() -> EventLog:
    """In-memory event log (fresh per test)."""
    return EventLog(db_path=Path(":memory:"))


@pytest.fixture()
def file_log(tmp_path: Path) -> EventLog:
    """File-backed event log in a temp directory."""
    return EventLog(db_path=tmp_path / "events.db")


# ---------------------------------------------------------------------------
# EventKind enum
# ---------------------------------------------------------------------------


class TestEventKind:
    def test_values_are_strings(self):
        assert EventKind.TOOL_CALL.value == "tool.call"
        assert EventKind.SESSION_START.value == "session.start"

    def test_all_kinds_unique(self):
        values = [k.value for k in EventKind]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# EventRecord
# ---------------------------------------------------------------------------


class TestEventRecord:
    def test_from_row(self):
        row = (1, "s1", 1, "tool.call", 1000.0, '{"tool": "read_file"}', None)
        rec = EventRecord.from_row(row)
        assert rec.id == 1
        assert rec.session_id == "s1"
        assert rec.seq == 1
        assert rec.kind == "tool.call"
        assert rec.timestamp == 1000.0
        assert rec.data == {"tool": "read_file"}
        assert rec.snapshot is None

    def test_from_row_with_snapshot(self):
        row = (2, "s1", 2, "snapshot.taken", 2000.0, "{}", "abc123")
        rec = EventRecord.from_row(row)
        assert rec.snapshot == "abc123"

    def test_from_row_empty_data(self):
        row = (3, "s1", 3, "info", 3000.0, "", None)
        rec = EventRecord.from_row(row)
        assert rec.data == {}


# ---------------------------------------------------------------------------
# Basic append + get_events
# ---------------------------------------------------------------------------


class TestAppend:
    def test_append_returns_seq_starting_at_1(self, log: EventLog):
        seq = log.append("s1", EventKind.TOOL_CALL, {"tool": "bash"})
        assert seq == 1

    def test_seq_increments_per_session(self, log: EventLog):
        s1 = log.append("s1", EventKind.TOOL_CALL)
        s2 = log.append("s1", EventKind.TOOL_RESULT)
        assert s1 == 1
        assert s2 == 2

    def test_seq_independent_across_sessions(self, log: EventLog):
        log.append("s1", EventKind.TOOL_CALL)
        seq_s2 = log.append("s2", EventKind.TOOL_CALL)
        assert seq_s2 == 1  # s2 starts at 1 independently

    def test_append_string_kind(self, log: EventLog):
        seq = log.append("s1", "custom.event", {"foo": "bar"})
        assert seq == 1
        events = log.get_events("s1")
        assert events[0].kind == "custom.event"

    def test_append_with_snapshot(self, log: EventLog):
        log.append("s1", EventKind.SNAPSHOT_TAKEN, snapshot="deadbeef" * 5)
        events = log.get_events("s1")
        assert events[0].snapshot == "deadbeef" * 5

    def test_append_custom_timestamp(self, log: EventLog):
        ts = 1_700_000_000.0
        log.append("s1", EventKind.INFO, timestamp=ts)
        events = log.get_events("s1")
        assert events[0].timestamp == ts


class TestGetEvents:
    def test_get_events_empty_session(self, log: EventLog):
        assert log.get_events("nonexistent") == []

    def test_get_events_returns_in_seq_order(self, log: EventLog):
        for i in range(5):
            log.append("s1", EventKind.TOOL_CALL, {"i": i})
        events = log.get_events("s1")
        assert [e.seq for e in events] == [1, 2, 3, 4, 5]

    def test_get_events_filter_by_kind(self, log: EventLog):
        log.append("s1", EventKind.TOOL_CALL)
        log.append("s1", EventKind.FILE_WRITE)
        log.append("s1", EventKind.TOOL_CALL)
        events = log.get_events("s1", kind=EventKind.TOOL_CALL)
        assert len(events) == 2
        assert all(e.kind == "tool.call" for e in events)

    def test_get_events_from_seq(self, log: EventLog):
        for i in range(5):
            log.append("s1", EventKind.INFO)
        events = log.get_events("s1", from_seq=3)
        assert [e.seq for e in events] == [4, 5]

    def test_get_events_to_seq(self, log: EventLog):
        for i in range(5):
            log.append("s1", EventKind.INFO)
        events = log.get_events("s1", to_seq=3)
        assert [e.seq for e in events] == [1, 2, 3]

    def test_get_events_range(self, log: EventLog):
        for i in range(10):
            log.append("s1", EventKind.INFO)
        events = log.get_events("s1", from_seq=3, to_seq=6)
        assert [e.seq for e in events] == [4, 5, 6]

    def test_get_events_limit(self, log: EventLog):
        for i in range(10):
            log.append("s1", EventKind.INFO)
        events = log.get_events("s1", limit=3)
        assert len(events) == 3

    def test_data_round_trips(self, log: EventLog):
        payload = {"tool": "write_file", "path": "foo.py", "size": 1024}
        log.append("s1", EventKind.FILE_WRITE, data=payload)
        events = log.get_events("s1")
        assert events[0].data == payload


# ---------------------------------------------------------------------------
# get_diff
# ---------------------------------------------------------------------------


class TestGetDiff:
    def test_diff_returns_only_tool_and_file_events(self, log: EventLog):
        log.append("s1", EventKind.SESSION_START)  # excluded
        log.append("s1", EventKind.TOOL_CALL, {"tool": "bash"})
        log.append("s1", EventKind.LLM_TURN_START)  # excluded
        log.append("s1", EventKind.FILE_WRITE, {"path": "foo.py"})
        log.append("s1", EventKind.TOOL_RESULT)  # excluded
        log.append("s1", EventKind.SNAPSHOT_TAKEN, snapshot="abc")
        events = log.get_diff("s1")
        kinds = {e.kind for e in events}
        assert "tool.call" in kinds
        assert "file.write" in kinds
        assert "snapshot.taken" in kinds
        assert "session.start" not in kinds
        assert "llm.turn_start" not in kinds
        assert "tool.result" not in kinds

    def test_diff_respects_from_to(self, log: EventLog):
        for i in range(10):
            log.append("s1", EventKind.TOOL_CALL, {"i": i})
        diff = log.get_diff("s1", from_seq=3, to_seq=7)
        assert [e.seq for e in diff] == [4, 5, 6, 7]

    def test_diff_empty_when_no_relevant_events(self, log: EventLog):
        log.append("s1", EventKind.SESSION_START)
        log.append("s1", EventKind.LLM_TURN_END)
        assert log.get_diff("s1") == []


# ---------------------------------------------------------------------------
# get_last_seq + count
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_get_last_seq_zero_for_new_session(self, log: EventLog):
        assert log.get_last_seq("nosuch") == 0

    def test_get_last_seq(self, log: EventLog):
        for _ in range(5):
            log.append("s1", EventKind.INFO)
        assert log.get_last_seq("s1") == 5

    def test_count_zero_for_new_session(self, log: EventLog):
        assert log.count("nosuch") == 0

    def test_count(self, log: EventLog):
        for _ in range(7):
            log.append("s1", EventKind.INFO)
        assert log.count("s1") == 7


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_events_survive_reopen(self, tmp_path: Path):
        db_path = tmp_path / "events.db"
        log1 = EventLog(db_path=db_path)
        log1.append("s1", EventKind.TOOL_CALL, {"tool": "bash"})
        log1.append("s1", EventKind.TOOL_RESULT, {"ok": True})
        log1.close()

        log2 = EventLog(db_path=db_path)
        events = log2.get_events("s1")
        assert len(events) == 2
        assert events[0].kind == "tool.call"
        log2.close()

    def test_seq_resumes_after_reopen(self, tmp_path: Path):
        db_path = tmp_path / "events.db"
        log1 = EventLog(db_path=db_path)
        log1.append("s1", EventKind.INFO)
        log1.append("s1", EventKind.INFO)
        log1.close()

        log2 = EventLog(db_path=db_path)
        # seq cache should be restored from DB
        seq = log2.append("s1", EventKind.INFO)
        assert seq == 3
        log2.close()

    def test_parent_dirs_created(self, tmp_path: Path):
        db_path = tmp_path / "deeply" / "nested" / "dir" / "events.db"
        log = EventLog(db_path=db_path)
        log.append("s1", EventKind.INFO)
        log.close()
        assert db_path.exists()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_appends_unique_seqs(self, log: EventLog):
        """Multiple threads appending to the same session get unique seqs."""
        seqs: list = []
        errors: list = []
        lock = threading.Lock()

        def worker():
            try:
                for _ in range(20):
                    seq = log.append("shared", EventKind.INFO)
                    with lock:
                        seqs.append(seq)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert len(seqs) == 100
        assert len(set(seqs)) == 100  # all unique

    def test_concurrent_different_sessions(self, log: EventLog):
        """Threads writing to different sessions don't interfere."""
        results: dict = {}
        lock = threading.Lock()

        def worker(session_id: str):
            for _ in range(10):
                log.append(session_id, EventKind.INFO)
            count = log.count(session_id)
            with lock:
                results[session_id] = count

        threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(5):
            assert results[f"s{i}"] == 10
