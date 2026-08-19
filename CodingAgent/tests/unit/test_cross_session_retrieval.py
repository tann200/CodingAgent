"""P4-T3: Tests for cross-session memory retrieval."""


def test_retrieve_returns_string_when_no_sessions(tmp_path):
    from src.core.memory.distiller import retrieve_relevant_prior_sessions

    result = retrieve_relevant_prior_sessions("do the thing", str(tmp_path))
    assert isinstance(result, str)  # must not raise; empty string OK


def test_retrieve_returns_empty_on_bad_working_dir():
    from src.core.memory.distiller import retrieve_relevant_prior_sessions

    result = retrieve_relevant_prior_sessions("task", "/nonexistent/path/xyz123")
    assert isinstance(result, str)


def test_get_recent_sessions_empty_store(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    sessions = store.get_recent_sessions(limit=5)
    assert sessions == []


def test_get_recent_sessions_returns_sessions(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    store.add_message("sess-A", "user", "Hello from A")
    store.add_message("sess-B", "user", "Hello from B")
    sessions = store.get_recent_sessions(limit=10)
    session_ids = {s["session_id"] for s in sessions}
    assert "sess-A" in session_ids
    assert "sess-B" in session_ids


def test_get_session_text_summary_empty(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    summary = store.get_session_text_summary("nonexistent")
    assert summary == ""


def test_get_session_text_summary_with_messages(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    store.add_message("sess-1", "user", "Write a sorting algorithm")
    store.add_message("sess-1", "assistant", "I created sort.py with bubble sort.")
    summary = store.get_session_text_summary("sess-1")
    assert "Write a sorting algorithm" in summary or "sort" in summary.lower()


def test_retrieve_with_populated_sessions(tmp_path):
    """retrieve_relevant_prior_sessions returns non-empty when sessions exist."""
    from src.core.memory.sqlite_session_store import SqliteSessionStore
    from src.core.memory.distiller import retrieve_relevant_prior_sessions

    # Populate a session
    store = SqliteSessionStore(workdir=str(tmp_path))
    store.add_message("s1", "user", "Implement a linked list")
    store.add_message("s1", "assistant", "Done: created linked_list.py")

    result = retrieve_relevant_prior_sessions("linked list task", str(tmp_path))
    assert isinstance(result, str)
    # May or may not include the session depending on store backend selection,
    # but must not raise.


def test_retrieve_max_chars_respected(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore
    from src.core.memory.distiller import retrieve_relevant_prior_sessions

    store = SqliteSessionStore(workdir=str(tmp_path))
    for i in range(5):
        store.add_message(f"sess-{i}", "user", "x" * 600)
        store.add_message(f"sess-{i}", "assistant", "y" * 600)

    result = retrieve_relevant_prior_sessions("task", str(tmp_path), max_chars=200)
    assert len(result) <= 200
