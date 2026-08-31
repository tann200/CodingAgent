"""PERF-01: streaming pagination for large JSONL sessions.

Locks the acceptance criterion: a session larger than 10,000 records is
traversable *without silent truncation*, and paged traversal stays
memory-bounded by page size.
"""

import json
from pathlib import Path

from src.core.memory.jsonl_session_store import JsonlSessionStore

_TOTAL = 12_000  # exceeds the retired _MAX_RECORDS silent cap (10_000)


def _write_sessions(dir_path: Path, session_id: str, count: int) -> int:
    """Bulk-write *count* message lines to a session file (fast, no fsync)."""
    file_path = dir_path / f"{session_id}.jsonl"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        for i in range(count):
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": f"msg-{i}",
                        "ts": f"t-{i}",
                    }
                )
                + "\n"
            )
    return count


def _build_store(tmp_path: Path, count: int = _TOTAL):
    store = JsonlSessionStore(workdir=str(tmp_path))
    written = _write_sessions(store._get_sessions_dir(), "large", count)
    return store, written


def test_iter_records_traverses_large_session_without_truncation(tmp_path):
    store, total = _build_store(tmp_path)
    records = list(store.iter_records("large"))
    assert len(records) == total == _TOTAL
    # Full traversal reaches the final record — no 10_000-cap silent drop.
    assert records[-1]["content"] == f"msg-{_TOTAL - 1}"


def test_read_all_records_returns_full_session_without_truncation(tmp_path):
    store, total = _build_store(tmp_path)
    assert total > 10_000  # proves the >10k scenario
    records = store._read_all_records("large")
    assert len(records) == _TOTAL


def test_read_page_bounded_and_has_more(tmp_path):
    store, _ = _build_store(tmp_path)
    page, has_more = store.read_page("large", page_size=50, offset=0)
    assert len(page) == 50  # memory bounded by page size
    assert has_more is True
    assert page[0]["content"] == "msg-0"


def test_read_page_last_page_has_no_more(tmp_path):
    store, _ = _build_store(tmp_path)
    page, has_more = store.read_page("large", page_size=50, offset=_TOTAL - 50)
    assert len(page) == 50
    assert has_more is False
    assert page[-1]["content"] == f"msg-{_TOTAL - 1}"


def test_read_page_pages_cover_every_record_exactly_once(tmp_path):
    store, total = _build_store(tmp_path)
    seen: list = []
    offset = 0
    page_size = 100
    while True:
        page, has_more = store.read_page("large", page_size=page_size, offset=offset)
        seen.extend(r["content"] for r in page)
        offset += len(page)
        if not has_more:
            break
    assert seen == [f"msg-{i}" for i in range(total)]


def test_read_page_empty_session_has_no_more(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    page, has_more = store.read_page("absent", page_size=10)
    assert page == []
    assert has_more is False


def test_iter_records_skips_blank_and_malformed_lines(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    file_path = store._get_sessions_dir() / "s1.jsonl"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps({"type": "message", "role": "user", "content": "a"})
        + "\n\n"
        + "not-json\n"
        + json.dumps({"type": "message", "role": "assistant", "content": "b"})
        + "\n",
        encoding="utf-8",
    )
    records = list(store.iter_records("s1"))
    assert [r["content"] for r in records] == ["a", "b"]


def test_read_page_is_still_consistent_with_get_messages(tmp_path):
    store, total = _build_store(tmp_path)
    messages = store.get_messages("large")
    assert len(messages) == total  # get_messages now full, not silently capped
    assert messages[0]["content"] == "msg-0"


def test_read_page_validates_page_size_and_offset(tmp_path):
    store = JsonlSessionStore(workdir=str(tmp_path))
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
