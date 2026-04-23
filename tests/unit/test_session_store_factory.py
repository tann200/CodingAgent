"""Unit tests for session store factory behaviour."""

from src.core.memory.session_store import get_session_store


def test_explicit_sqlite_backend_returns_raw(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = get_session_store(workdir=str(tmp_path), backend="sqlite")
    assert isinstance(store, SqliteSessionStore)


def test_explicit_jsonl_backend_returns_raw(tmp_path):
    from src.core.memory.jsonl_session_store import JsonlSessionStore

    store = get_session_store(workdir=str(tmp_path), backend="jsonl")
    assert isinstance(store, JsonlSessionStore)
