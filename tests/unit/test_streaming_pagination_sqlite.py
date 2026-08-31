"""PERF-01 follow-up: Sqlite parity for streaming pagination + lazy summary.

Locks the acceptance criteria that (a) the Sqlite store exposes the same
iter_records / read_page cursor API as the JSONL store, so both backends
paginate large sessions with bounded memory and an explicit has_more signal,
and (b) JsonlSessionStore.get_session_summary counts from the lazy iterator
without materialising the whole session.
"""

from src.core.memory.jsonl_session_store import JsonlSessionStore
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


def test_jsonl_get_session_summary_counts_lazily_matches_full_read(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    sess_dir = store._get_sessions_dir()
    sess_dir.mkdir(parents=True, exist_ok=True)
    import json

    with open(sess_dir / "analytics.jsonl", "w", encoding="utf-8") as f:
        for i in range(70):
            kind = (
                "message"
                if i % 2 == 0
                else "tool_call"
                if i % 3 == 0
                else "error"
                if i % 5 == 0
                else "decision"
            )
            f.write(json.dumps({"type": kind, "content": f"r-{i}"}) + "\n")

    summary = store.get_session_summary("analytics")
    records = list(store.iter_records("analytics"))
    assert summary["messages"] == summary["message_count"] == sum(
        1 for r in records if r.get("type") == "message"
    )
    assert summary["tool_calls"] == summary["tool_call_count"] == sum(
        1 for r in records if r.get("type") == "tool_call"
    )
    assert summary["errors"] == summary["error_count"] == sum(
        1 for r in records if r.get("type") == "error"
    )
    assert summary["decisions"] == sum(
        1 for r in records if r.get("type") == "decision"
    )
    assert summary["plans"] == 0
    assert summary["session_id"] == "analytics"
