"""tests/unit/test_session_fork_revert.py — S5-A / S5-B

Unit tests for SessionStore.fork_session() and SessionStore.revert_session().
"""

from __future__ import annotations

import pytest
from pathlib import Path

from src.core.memory.session_store import SessionStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(workdir=str(tmp_path))


def _populate(store: SessionStore, session_id: str) -> None:
    """Add a minimal set of rows for *session_id*."""
    store.add_message(session_id, "user", "hello")
    store.add_message(session_id, "assistant", "world")
    store.add_tool_call(
        session_id, "read_file", {"path": "foo.py"}, '{"ok": true}', True
    )
    store.add_error(session_id, "ValueError", "oops", '{"k": 1}')
    store.add_plan(session_id, '["step1", "step2"]', "active")
    store.add_decision(session_id, "use write_file", "fastest path")


# ---------------------------------------------------------------------------
# S5-A — fork_session
# ---------------------------------------------------------------------------


class TestForkSession:
    def test_sf1_fork_creates_new_session(self, store: SessionStore) -> None:
        """SF-1: fork_session returns a new, distinct session_id."""
        _populate(store, "src")
        fork_id = store.fork_session("src")
        assert fork_id != "src"
        assert fork_id in store.list_sessions()

    def test_sf2_fork_copies_messages(self, store: SessionStore) -> None:
        """SF-2: Forked session has the same messages as the source."""
        _populate(store, "src")
        fork_id = store.fork_session("src")
        src_msgs = store.get_messages("src")
        fork_msgs = store.get_messages(fork_id)
        assert len(fork_msgs) == len(src_msgs)
        assert [m["content"] for m in fork_msgs] == [m["content"] for m in src_msgs]

    def test_sf3_fork_copies_tool_calls(self, store: SessionStore) -> None:
        """SF-3: Forked session has the same tool_calls as the source."""
        _populate(store, "src")
        fork_id = store.fork_session("src")
        assert len(store.get_tool_calls(fork_id)) == len(store.get_tool_calls("src"))

    def test_sf4_fork_copies_plans(self, store: SessionStore) -> None:
        """SF-4: Forked session has the same plans as the source."""
        _populate(store, "src")
        fork_id = store.fork_session("src")
        assert len(store.get_plans(fork_id)) == len(store.get_plans("src"))

    def test_sf5_fork_copies_decisions(self, store: SessionStore) -> None:
        """SF-5: Forked session has the same decisions as the source."""
        _populate(store, "src")
        fork_id = store.fork_session("src")
        assert len(store.get_decisions(fork_id)) == len(store.get_decisions("src"))

    def test_sf6_fork_independence(self, store: SessionStore) -> None:
        """SF-6: Writing to the fork does not affect the source."""
        _populate(store, "src")
        fork_id = store.fork_session("src")
        store.add_message(fork_id, "user", "fork-only message")
        src_msgs = store.get_messages("src")
        fork_msgs = store.get_messages(fork_id)
        assert len(fork_msgs) == len(src_msgs) + 1
        assert all(m["content"] != "fork-only message" for m in src_msgs)

    def test_sf7_fork_with_explicit_id(self, store: SessionStore) -> None:
        """SF-7: Caller can supply an explicit fork_id."""
        _populate(store, "src")
        fork_id = store.fork_session("src", fork_id="my-fork")
        assert fork_id == "my-fork"
        assert "my-fork" in store.list_sessions()

    def test_sf8_fork_nonexistent_raises(self, store: SessionStore) -> None:
        """SF-8: fork_session raises ValueError when source does not exist."""
        with pytest.raises(ValueError, match="does not exist"):
            store.fork_session("ghost-session")

    def test_sf9_source_unchanged_after_fork(self, store: SessionStore) -> None:
        """SF-9: Source session is unchanged by the fork operation."""
        _populate(store, "src")
        src_before = store.get_session_summary("src")
        store.fork_session("src")
        src_after = store.get_session_summary("src")
        assert src_before == src_after

    def test_sf10_fork_of_empty_tool_calls_session(self, store: SessionStore) -> None:
        """SF-10: fork_session works when source only has messages (no tool_calls)."""
        store.add_message("msg-only", "user", "hi")
        fork_id = store.fork_session("msg-only")
        msgs = store.get_messages(fork_id)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hi"


# ---------------------------------------------------------------------------
# S5-B — revert_session
# ---------------------------------------------------------------------------


class TestRevertSession:
    def test_sr1_revert_clears_all_tables(self, store: SessionStore) -> None:
        """SR-1: revert_session deletes rows from all five tables by default."""
        _populate(store, "s1")
        result = store.revert_session("s1")
        assert result["ok"] is True
        assert store.get_messages("s1") == []
        assert store.get_tool_calls("s1") == []
        assert store.get_errors("s1") == []
        assert store.get_plans("s1") == []
        assert store.get_decisions("s1") == []

    def test_sr2_revert_keep_messages(self, store: SessionStore) -> None:
        """SR-2: keep_messages=True preserves messages but clears other tables."""
        _populate(store, "s1")
        result = store.revert_session("s1", keep_messages=True)
        assert result["ok"] is True
        # Messages intact
        assert len(store.get_messages("s1")) == 2
        # Other artefacts cleared
        assert store.get_tool_calls("s1") == []
        assert store.get_errors("s1") == []
        assert store.get_plans("s1") == []
        assert store.get_decisions("s1") == []

    def test_sr3_revert_returns_deleted_counts(self, store: SessionStore) -> None:
        """SR-3: result['deleted'] maps table → rows deleted."""
        _populate(store, "s1")
        result = store.revert_session("s1")
        deleted = result["deleted"]
        assert deleted["messages"] == 2
        assert deleted["tool_calls"] == 1
        assert deleted["errors"] == 1
        assert deleted["plans"] == 1
        assert deleted["decisions"] == 1

    def test_sr4_revert_nonexistent_session_is_ok(self, store: SessionStore) -> None:
        """SR-4: Reverting a session with no rows succeeds with zero deletes."""
        result = store.revert_session("ghost")
        assert result["ok"] is True
        assert all(v == 0 for v in result["deleted"].values())

    def test_sr5_revert_does_not_affect_other_sessions(
        self, store: SessionStore
    ) -> None:
        """SR-5: Reverting one session leaves other sessions untouched."""
        _populate(store, "s1")
        _populate(store, "s2")
        store.revert_session("s1")
        assert len(store.get_messages("s2")) == 2
        assert len(store.get_tool_calls("s2")) == 1

    def test_sr6_double_revert_is_idempotent(self, store: SessionStore) -> None:
        """SR-6: Calling revert_session twice on the same session is safe."""
        _populate(store, "s1")
        store.revert_session("s1")
        result2 = store.revert_session("s1")
        assert result2["ok"] is True
        assert all(v == 0 for v in result2["deleted"].values())

    def test_sr7_fork_then_revert_source_leaves_fork_intact(
        self, store: SessionStore
    ) -> None:
        """SR-7: Reverting the source after a fork leaves the forked copy intact."""
        _populate(store, "src")
        fork_id = store.fork_session("src")
        store.revert_session("src")
        # Source cleared
        assert store.get_messages("src") == []
        # Fork untouched
        assert len(store.get_messages(fork_id)) == 2
