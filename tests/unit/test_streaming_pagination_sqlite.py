"""PERF-01 follow-up: Sqlite streaming pagination + summary.

Locks the acceptance criteria that (a) the SQLite store exposes
iter_records / read_page cursor APIs so large sessions paginate with
bounded memory and an explicit has_more signal, and (b) get_session_summary
counts match full records.
"""

from src.core.memory.sqlite_session_store import SqliteSessionStore


def _write_sqlite(store, n: int, session_id: str = "s1") -> int:
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        store.add_message(session_id, role, f"msg-{i}")
    return n


def test_sqlite_iter_records_yields_all_messages_in_order(tmp_path):
    store = SqliteSessionStore(workdir=str(tmp_path))
    _write_sqlite(store, 300)
    contents = [r["content"] for r in store.iter_records("s1")]
    assert contents == [f"msg-{i}" for i in range(300)]
    assert contents[0] == "msg-0"
    assert contents[-1] == "msg-299"


def test_sqlite_read_page_bounded_and_has_more(tmp_path):
    store = SqliteSessionStore(workdir=str(tmp_path))
    _write_sqlite(store, 100)
    page, has_more = store.read_page("s1", page_size=20, offset=0)
    assert len(page) == 20
    assert has_more is True
    assert page[0]["content"] == "msg-0"


def test_sqlite_read_page_last_page_has_no_more(tmp_path):
    store = SqliteSessionStore(workdir=str(tmp_path))
    _write_sqlite(store, 100)
    page, has_more = store.read_page("s1", page_size=20, offset=80)
    assert len(page) == 20
    assert has_more is False
    assert page[-1]["content"] == "msg-99"


def test_sqlite_read_page_pages_cover_every_message_exactly_once(tmp_path):
    store = SqliteSessionStore(workdir=str(tmp_path))
    total = _write_sqlite(store, 257)
    seen = []
    offset = 0
    page_size = 40
    while True:
        page, has_more = store.read_page("s1", page_size=page_size, offset=offset)
        seen.extend(r["content"] for r in page)
        offset += len(page)
        if not has_more:
            break
    assert seen == [f"msg-{i}" for i in range(total)]


def test_sqlite_read_page_empty_session_has_no_more(tmp_path):
    store = SqliteSessionStore(workdir=str(tmp_path))
    page, has_more = store.read_page("absent", page_size=10)
    assert page == []
    assert has_more is False


def test_sqlite_iter_records_matches_get_messages(tmp_path):
    store = SqliteSessionStore(workdir=str(tmp_path))
    _write_sqlite(store, 150, session_id="multi")
    streamed = [r["content"] for r in store.iter_records("multi")]
    messages = [m["content"] for m in store.get_messages("multi")]
    assert streamed == messages == [f"msg-{i}" for i in range(150)]


def test_sqlite_read_page_validates_args(tmp_path):
    store = SqliteSessionStore(workdir=str(tmp_path))
    try:
        store.read_page("x", page_size=0)
    except ValueError as exc:
        assert "page_size" in str(exc)
    else:
        raise AssertionError("expected ValueError for page_size=0")
    try:
        store.read_page("x", page_size=5, offset=-1)
    except ValueError as exc:
        assert "offset" in str(exc)
    else:
        raise AssertionError("expected ValueError for offset=-1")


def test_sqlite_get_session_summary_matches_counts(tmp_path):
    store = SqliteSessionStore(workdir=str(tmp_path))
    for i in range(40):
        if i % 2 == 0:
            store.add_message("analytics", "user", f"m-{i}")
        elif i % 3 == 0:
            store.add_tool_call("analytics", "read_file", {"path": f"f-{i}"}, {"ok": True})
        elif i % 5 == 0:
            store.add_error("analytics", "runtime", f"e-{i}")
        else:
            store.add_decision("analytics", f"d-{i}", "rationale")

    summary = store.get_session_summary("analytics")
    assert summary["message_count"] == 20
    assert summary["tool_call_count"] == 7
    assert summary["error_count"] == 3
    assert summary["session_id"] == "analytics"
