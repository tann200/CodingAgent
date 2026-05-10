from datetime import datetime

from src.core.orchestration.graph.nodes.planning_helpers import (
    hydrate_repo_context_from_index,
    plan_is_resumable,
)


def test_plan_is_resumable_accepts_matching_recent_plan():
    logger = type("L", (), {"info": lambda *a, **k: None})()

    result = plan_is_resumable(
        data={"task": "task", "saved_at": "2024-01-01T00:00:00", "plan": [1]},
        current_task="task",
        resume_ttl_seconds=1800,
        logger=logger,
        now_fn=lambda: datetime(2024, 1, 1, 0, 10, 0),
        datetime_fromisoformat_fn=datetime.fromisoformat,
    )

    assert result is True


def test_plan_is_resumable_rejects_expired_plan():
    logged = []
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    result = plan_is_resumable(
        data={"task": "task", "saved_at": "2024-01-01T00:00:00", "plan": [1]},
        current_task="task",
        resume_ttl_seconds=1800,
        logger=logger,
        now_fn=lambda: datetime(2024, 1, 1, 1, 0, 0),
        datetime_fromisoformat_fn=datetime.fromisoformat,
    )

    assert result is False
    assert logged == ["planning_node: saved plan is %.0fs old (> %ss TTL) — not resuming"]


def test_plan_is_resumable_resume_session_only_requires_plan():
    logger = type("L", (), {"info": lambda *a, **k: None})()

    assert plan_is_resumable(
        data={"task": "other", "plan": [1]},
        current_task="task",
        resume_ttl_seconds=1800,
        logger=logger,
        resume_session=True,
    ) is True


def test_hydrate_repo_context_from_index_fills_missing_files_and_symbols():
    logged = []
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0]), "debug": lambda *a, **k: None},
    )()

    files, symbols, indexed = hydrate_repo_context_from_index(
        working_dir="/tmp/project",
        task="fix bug",
        relevant_files=[],
        key_symbols=[],
        get_symbols_for_task_fn=lambda wd, task, max_results=8: [
            {"file_path": "src/a.py", "name": "foo"},
            {"file_path": "src/b.py", "name": "bar"},
            {"file_path": "src/a.py", "name": "foo"},
        ],
        logger=logger,
    )

    assert files == ["src/a.py", "src/b.py"]
    assert symbols == ["foo", "bar"]
    assert len(indexed) == 3
    assert logged == ["planning_node: hydrated repo context from index (%d files, %d symbols)"]


def test_hydrate_repo_context_from_index_preserves_existing_context():
    logger = type("L", (), {"info": lambda *a, **k: None, "debug": lambda *a, **k: None})()

    files, symbols, indexed = hydrate_repo_context_from_index(
        working_dir="/tmp/project",
        task="fix bug",
        relevant_files=["src/existing.py"],
        key_symbols=["Existing"],
        get_symbols_for_task_fn=lambda wd, task, max_results=8: [{"file_path": "src/a.py", "name": "foo"}],
        logger=logger,
    )

    assert files == ["src/existing.py"]
    assert symbols == ["Existing"]
    assert indexed == []


def test_hydrate_repo_context_from_index_returns_existing_context_on_error():
    logged = []
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: None, "debug": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    files, symbols, indexed = hydrate_repo_context_from_index(
        working_dir="/tmp/project",
        task="fix bug",
        relevant_files=["src/existing.py"],
        key_symbols=[],
        get_symbols_for_task_fn=lambda wd, task, max_results=8: (_ for _ in ()).throw(RuntimeError("boom")),
        logger=logger,
    )

    assert files == ["src/existing.py"]
    assert symbols == []
    assert indexed == []
    assert logged == ["planning_node: repo-context hydration failed (non-critical): %s"]
