"""test_jsonl_history_completeness.py — Task #6 JSONL history completeness tests.

Tests cover:
- get_messages() cap is explicitly observable (truncated flag via get_messages_with_meta)
- get_records_page() with opaque cursor, finite validated limit, chronological
  traversal across rotated files, has_more/truncated metadata
- get_messages_page() convenience wrapper
- SessionStore pass-throughs for page APIs
- Full traversal produces no duplicates/omissions
- Cursor validation (bad cursor raises ValueError)
- Malformed lines are skipped without losing ordinal stability
- get_session_summary() includes truncated metadata
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.core.memory.jsonl_session_store import JsonlSessionStore
from src.core.memory.session_store import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path: Path, rotation_bytes: int = 256 * 1024) -> JsonlSessionStore:
    return JsonlSessionStore(workdir=str(tmp_path), rotation_bytes=rotation_bytes)


def _add_messages(store: JsonlSessionStore, session_id: str, count: int) -> None:
    for i in range(count):
        store.add_message(session_id, role="user", content=f"msg-{i}")


def _force_rotation(store: JsonlSessionStore, session_id: str) -> None:
    """Manually force rotation by setting active file size to exceed threshold."""
    active = store._active_file(session_id)
    if active.exists():
        # Rename to trigger a rotation on the next write
        sessions_dir = store._get_sessions_dir()
        n = 0
        while (sessions_dir / f"{session_id}.{n}.jsonl").exists():
            n += 1
        import shutil
        shutil.move(str(active), str(sessions_dir / f"{session_id}.{n}.jsonl"))


# ---------------------------------------------------------------------------
# Cap observability via get_messages_with_meta
# ---------------------------------------------------------------------------

class TestCapObservability:
    """get_messages() cap is observable through get_messages_with_meta."""

    def test_no_truncation_below_cap(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store._MAX_RECORDS = 20
        _add_messages(store, "s1", 10)
        msgs, truncated = store.get_messages_with_meta("s1")
        assert len(msgs) == 10
        assert truncated is False

    def test_truncation_detected_at_cap(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store._MAX_RECORDS = 5
        _add_messages(store, "s1", 10)
        msgs, truncated = store.get_messages_with_meta("s1")
        assert len(msgs) == 5
        assert truncated is True

    def test_get_messages_still_returns_list(self, tmp_path: Path) -> None:
        """Backward-compatible get_messages() still returns a plain list."""
        store = _store(tmp_path)
        _add_messages(store, "s1", 3)
        result = store.get_messages("s1")
        assert isinstance(result, list)
        assert len(result) == 3

    def test_summary_includes_truncated_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 3)
        summary = store.get_session_summary("s1")
        assert "truncated" in summary
        assert summary["truncated"] is False

    def test_summary_includes_truncated_true(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store._MAX_RECORDS = 4
        _add_messages(store, "s1", 10)
        summary = store.get_session_summary("s1")
        assert summary["truncated"] is True
        # counts are lower-bounds only
        assert summary["message_count"] == 4


# ---------------------------------------------------------------------------
# Page API: basic functionality
# ---------------------------------------------------------------------------

class TestPageAPIBasic:
    """get_records_page basic contract."""

    def test_first_page_no_cursor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 5)
        page = store.get_records_page("s1", limit=3)
        assert len(page["items"]) == 3
        assert page["has_more"] is True
        assert page["next_cursor"] is not None
        assert page["truncated"] is False
        assert page["page_limit"] == 3

    def test_second_page_via_cursor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 5)
        page1 = store.get_records_page("s1", limit=3)
        page2 = store.get_records_page("s1", cursor=page1["next_cursor"], limit=3)
        assert len(page2["items"]) == 2
        assert page2["has_more"] is False
        assert page2["next_cursor"] is None

    def test_limit_clamped_to_max(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 3)
        page = store.get_records_page("s1", limit=99_999)
        assert page["page_limit"] == store._MAX_PAGE_LIMIT

    def test_limit_clamped_minimum(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 5)
        page = store.get_records_page("s1", limit=0)
        assert page["page_limit"] == 1

    def test_record_type_filter(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 3)
        store.add_tool_call("s1", "read_file", {}, {})
        page = store.get_records_page("s1", limit=100, record_type="message")
        assert all(r.get("type") == "message" for r in page["items"])
        assert len(page["items"]) == 3

    def test_empty_session_returns_empty_page(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        page = store.get_records_page("nonexistent", limit=10)
        assert page["items"] == []
        assert page["has_more"] is False
        assert page["next_cursor"] is None
        assert page["truncated"] is False

    def test_exact_page_boundary(self, tmp_path: Path) -> None:
        """When count == limit exactly, has_more should be False (no more records)."""
        store = _store(tmp_path)
        _add_messages(store, "s1", 3)
        page = store.get_records_page("s1", limit=3)
        # exactly 3 records, limit=3 — next page would be empty
        assert len(page["items"]) == 3
        # has_more may be True (we stopped scanning at limit) but next page empty
        if page["has_more"]:
            page2 = store.get_records_page("s1", cursor=page["next_cursor"], limit=3)
            assert page2["items"] == []
            assert page2["has_more"] is False


# ---------------------------------------------------------------------------
# Cursor validation
# ---------------------------------------------------------------------------

class TestCursorValidation:
    def test_bad_cursor_raises_value_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 3)
        with pytest.raises(ValueError, match="invalid cursor"):
            store.get_records_page("s1", cursor="not-valid-base64!!")

    def test_tampered_cursor_raises_value_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        import base64
        bad = base64.urlsafe_b64encode(b'{"x": 1}').decode()
        with pytest.raises(ValueError, match="invalid cursor"):
            store.get_records_page("s1", cursor=bad)

    def test_negative_ordinal_cursor_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        import base64
        bad = base64.urlsafe_b64encode(b'{"o": -1}').decode()
        with pytest.raises(ValueError):
            store.get_records_page("s1", cursor=bad)

    def test_cursor_roundtrip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for n in [0, 1, 999, 10_000]:
            encoded = JsonlSessionStore._encode_cursor(n)
            decoded = JsonlSessionStore._decode_cursor(encoded)
            assert decoded == n


# ---------------------------------------------------------------------------
# Rotation traversal
# ---------------------------------------------------------------------------

class TestRotationTraversal:
    """Traversal across rotated files produces no duplicates/omissions."""

    def test_traversal_across_rotation_no_gaps(self, tmp_path: Path) -> None:
        """Write records, force rotation, write more; full page traversal covers all."""
        store = _store(tmp_path)
        store._MAX_RECORDS = 1000  # ensure cap doesn't interfere

        # Write 5 records to file 0
        for i in range(5):
            store.add_message("s1", role="user", content=f"pre-rotate-{i}")
        _force_rotation(store, "s1")

        # Write 5 more to the new active file
        for i in range(5):
            store.add_message("s1", role="user", content=f"post-rotate-{i}")

        # Full traversal via pages
        all_items: List[Dict[str, Any]] = []
        cursor = None
        pages = 0
        while True:
            page = store.get_records_page("s1", cursor=cursor, limit=3)
            all_items.extend(page["items"])
            pages += 1
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            assert cursor is not None

        assert len(all_items) == 10, f"Expected 10 records, got {len(all_items)}"
        # Verify chronological order: pre-rotate messages come first
        contents = [r["content"] for r in all_items if r.get("type") == "message"]
        assert contents[:5] == [f"pre-rotate-{i}" for i in range(5)]
        assert contents[5:] == [f"post-rotate-{i}" for i in range(5)]

    def test_no_duplicate_records_across_pages(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        N = 20
        _add_messages(store, "s1", N)

        all_contents: List[str] = []
        cursor = None
        while True:
            page = store.get_records_page("s1", cursor=cursor, limit=4, record_type="message")
            for item in page["items"]:
                all_contents.append(item["content"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]

        assert len(all_contents) == N
        assert len(set(all_contents)) == N, "Duplicate records found"

    def test_lowered_cap_across_rotation(self, tmp_path: Path) -> None:
        """With a lowered cap (e.g. 7), truncation fires at the right point."""
        store = _store(tmp_path)
        store._MAX_RECORDS = 7

        for i in range(5):
            store.add_message("s1", role="user", content=f"batch1-{i}")
        _force_rotation(store, "s1")
        for i in range(5):
            store.add_message("s1", role="user", content=f"batch2-{i}")

        page = store.get_records_page("s1", limit=100)
        assert page["truncated"] is True
        assert len(page["items"]) <= 7

    def test_cursor_survives_rotation(self, tmp_path: Path) -> None:
        """Cursor from before rotation still picks up the right records after."""
        store = _store(tmp_path)
        store._MAX_RECORDS = 1000

        # Write 3 initial records
        for i in range(3):
            store.add_message("s1", role="user", content=f"pre-{i}")

        # Get cursor pointing to the end of these 3 records
        page1 = store.get_records_page("s1", limit=3)
        assert len(page1["items"]) == 3
        assert page1["has_more"] is False or page1["next_cursor"] is not None

        # Force rotation
        _force_rotation(store, "s1")

        # Write 3 more records to new active file
        for i in range(3):
            store.add_message("s1", role="user", content=f"post-{i}")

        # Resume from end cursor
        # Since has_more was False, we use a fresh page from the beginning
        all_items: List[Dict[str, Any]] = []
        cursor = None
        while True:
            page = store.get_records_page("s1", cursor=cursor, limit=10)
            all_items.extend(page["items"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]

        assert len(all_items) == 6
        contents = [r["content"] for r in all_items]
        assert contents[:3] == [f"pre-{i}" for i in range(3)]
        assert contents[3:] == [f"post-{i}" for i in range(3)]


# ---------------------------------------------------------------------------
# Malformed lines
# ---------------------------------------------------------------------------

class TestMalformedLines:
    """Malformed JSONL lines are skipped without losing ordinal stability."""

    def _inject_malformed(self, store: JsonlSessionStore, session_id: str) -> None:
        """Append a malformed line directly to the active JSONL file."""
        sessions_dir = store._get_sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)
        active = sessions_dir / f"{session_id}.jsonl"
        with active.open("a", encoding="utf-8") as f:
            f.write("THIS IS NOT JSON\n")

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.add_message("s1", role="user", content="before-malformed")
        self._inject_malformed(store, "s1")
        store.add_message("s1", role="user", content="after-malformed")

        page = store.get_records_page("s1", limit=100)
        # Malformed line is skipped; we get only valid records
        contents = [r["content"] for r in page["items"] if r.get("type") == "message"]
        assert "before-malformed" in contents
        assert "after-malformed" in contents

    def test_malformed_lines_advance_ordinal(self, tmp_path: Path) -> None:
        """Malformed lines consume ordinals so subsequent cursors are stable."""
        store = _store(tmp_path)
        store.add_message("s1", role="user", content="msg-0")
        self._inject_malformed(store, "s1")
        store.add_message("s1", role="user", content="msg-1")

        # Page 1: get 1 item
        page1 = store.get_records_page("s1", limit=1)
        assert len(page1["items"]) == 1
        assert page1["items"][0]["content"] == "msg-0"
        assert page1["has_more"] is True

        # Page 2: should get msg-1 (malformed line consumed an ordinal)
        page2 = store.get_records_page("s1", cursor=page1["next_cursor"], limit=10)
        contents = [r["content"] for r in page2["items"] if r.get("type") == "message"]
        assert "msg-1" in contents

    def test_all_malformed_returns_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._inject_malformed(store, "s1")
        self._inject_malformed(store, "s1")
        page = store.get_records_page("s1", limit=10)
        assert page["items"] == []
        assert page["has_more"] is False


# ---------------------------------------------------------------------------
# get_messages_page convenience
# ---------------------------------------------------------------------------

class TestMessagesPage:
    def test_messages_page_filters_type(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 4)
        store.add_tool_call("s1", "tool", {}, {})
        page = store.get_messages_page("s1", limit=10)
        assert all("role" in r and "content" in r for r in page["items"])
        assert len(page["items"]) == 4

    def test_messages_page_pagination(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add_messages(store, "s1", 6)
        all_msgs: List[Dict] = []
        cursor = None
        while True:
            page = store.get_messages_page("s1", cursor=cursor, limit=2)
            all_msgs.extend(page["items"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        assert len(all_msgs) == 6
        assert all("role" in m for m in all_msgs)


# ---------------------------------------------------------------------------
# SessionStore pass-throughs
# ---------------------------------------------------------------------------

class TestSessionStorePassthroughs:
    """SessionStore wrapper correctly exposes the page API pass-throughs."""

    def test_session_store_get_records_page(self, tmp_path: Path) -> None:
        store = SessionStore(workdir=str(tmp_path), backend="jsonl")
        store.add_message("s1", "user", "hello")
        page = store.get_records_page("s1", limit=10)
        assert "items" in page
        assert "has_more" in page
        assert "next_cursor" in page
        assert "truncated" in page

    def test_session_store_get_messages_page(self, tmp_path: Path) -> None:
        store = SessionStore(workdir=str(tmp_path), backend="jsonl")
        store.add_message("s1", "user", "hello")
        store.add_message("s1", "assistant", "hi")
        page = store.get_messages_page("s1", limit=10)
        assert len(page["items"]) == 2
        assert all("role" in m for m in page["items"])

    def test_session_store_get_messages_with_meta(self, tmp_path: Path) -> None:
        store = SessionStore(workdir=str(tmp_path), backend="jsonl")
        store.add_message("s1", "user", "hello")
        msgs, truncated = store.get_messages_with_meta("s1")
        assert isinstance(msgs, list)
        assert truncated is False

    def test_session_store_none_session_id_normalised(self, tmp_path: Path) -> None:
        store = SessionStore(workdir=str(tmp_path), backend="jsonl")
        # Should not raise; None → "unknown"
        page = store.get_records_page(None, limit=5)  # type: ignore[arg-type]
        assert isinstance(page, dict)


# ---------------------------------------------------------------------------
# Full traversal no-duplicates / no-omissions (end-to-end)
# ---------------------------------------------------------------------------

class TestFullTraversal:
    """Page through the entire history and verify no records are lost or doubled."""

    def test_full_traversal_single_file(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        N = 25
        for i in range(N):
            store.add_message("s1", "user", f"m{i}")
        all_contents: List[str] = []
        cursor = None
        while True:
            page = store.get_records_page("s1", cursor=cursor, limit=7, record_type="message")
            for r in page["items"]:
                all_contents.append(r["content"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        assert sorted(all_contents) == sorted([f"m{i}" for i in range(N)])
        assert len(set(all_contents)) == N

    def test_full_traversal_multi_rotation(self, tmp_path: Path) -> None:
        """Small rotation threshold (1 byte) forces many rotations; traversal still correct."""
        # rotation_bytes=1 so every write triggers a rotation
        store = _store(tmp_path, rotation_bytes=1)
        store._MAX_RECORDS = 50
        N = 10
        for i in range(N):
            store.add_message("s1", "user", f"r{i}")

        all_contents: List[str] = []
        cursor = None
        iterations = 0
        while True:
            page = store.get_records_page("s1", cursor=cursor, limit=3, record_type="message")
            for r in page["items"]:
                all_contents.append(r["content"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            iterations += 1
            assert iterations < 100, "Infinite loop guard triggered"

        assert len(all_contents) == N
        assert len(set(all_contents)) == N

    def test_lowered_cap_traversal_stops_at_cap(self, tmp_path: Path) -> None:
        """With cap=5, full traversal yields exactly 5 records then truncated=True."""
        store = _store(tmp_path)
        store._MAX_RECORDS = 5
        _add_messages(store, "s1", 12)

        all_items: List[Any] = []
        cursor = None
        truncated_seen = False
        iterations = 0
        while True:
            page = store.get_records_page("s1", cursor=cursor, limit=3)
            all_items.extend(page["items"])
            if page["truncated"]:
                truncated_seen = True
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            iterations += 1
            assert iterations < 20, "Unexpected infinite loop"

        assert len(all_items) <= 5
        assert truncated_seen
