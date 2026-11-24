"""Unit tests for canonical SQLite session store factory behaviour (TASK-24)."""

from src.core.memory.session_store import SessionStore, get_session_store
from src.core.memory.sqlite_session_store import SqliteSessionStore


def test_explicit_sqlite_backend_returns_raw(tmp_path):
    store = get_session_store(workdir=str(tmp_path), backend="sqlite")
    assert isinstance(store, SqliteSessionStore)


def test_default_factory_returns_sqlite_backed_session_store(tmp_path):
    store = get_session_store(workdir=str(tmp_path))
    assert isinstance(store, SessionStore)
    assert isinstance(store._store, SqliteSessionStore)


def test_legacy_jsonl_backend_arg_routes_to_canonical_sqlite(tmp_path):
    store = get_session_store(workdir=str(tmp_path), backend="jsonl")
    assert isinstance(store, SqliteSessionStore)
