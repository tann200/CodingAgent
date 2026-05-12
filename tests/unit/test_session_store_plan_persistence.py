"""P3-T4: Tests for save_plan / load_plan in SqliteSessionStore."""
import pytest


def test_save_and_load_plan(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    plan = [{"description": "step1", "completed": False}]
    store.save_plan("sess-1", plan, "do the thing", 0)
    loaded = store.load_plan("sess-1")
    assert loaded is not None
    assert loaded["plan"] == plan
    assert loaded["task"] == "do the thing"
    assert loaded["current_step"] == 0


def test_load_plan_missing_returns_none(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    assert store.load_plan("nonexistent") is None


def test_save_plan_overwrites_on_same_session(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    plan_v1 = [{"description": "s1"}]
    plan_v2 = [{"description": "s1"}, {"description": "s2"}]
    store.save_plan("sess-1", plan_v1, "task A", 0)
    store.save_plan("sess-1", plan_v2, "task A", 1)
    loaded = store.load_plan("sess-1")
    assert loaded is not None
    assert loaded["plan"] == plan_v2
    assert loaded["current_step"] == 1


def test_schema_version_is_4(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    assert store.get_schema_version() == 4


def test_save_plan_multiple_sessions_isolated(tmp_path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=str(tmp_path))
    store.save_plan("sess-A", [{"description": "A"}], "task A", 0)
    store.save_plan("sess-B", [{"description": "B"}], "task B", 2)
    a = store.load_plan("sess-A")
    b = store.load_plan("sess-B")
    assert a["plan"] == [{"description": "A"}]
    assert b["current_step"] == 2
