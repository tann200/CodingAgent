from datetime import datetime
from pathlib import Path

from src.core.orchestration.graph.nodes.planning_helpers import (
    get_last_plan_path,
    load_last_plan,
    save_last_plan,
)


def test_get_last_plan_path_falls_back_to_codingagent_dir(tmp_path):
    path = get_last_plan_path(workdir=str(tmp_path))

    assert path == tmp_path / ".codingAgent" / "last_plan.json"


def test_get_last_plan_path_uses_agent_context_path_when_available(tmp_path):
    path = get_last_plan_path(
        workdir=str(tmp_path),
        agent_context_path_fn=lambda base: base / ".agent-context",
    )

    assert path == tmp_path / ".agent-context" / "last_plan.json"


def test_load_last_plan_returns_empty_dict_for_missing_file(tmp_path):
    logger = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()

    result = load_last_plan(
        workdir=str(tmp_path),
        get_last_plan_path_fn=lambda workdir: Path(workdir) / ".codingAgent" / "last_plan.json",
        logger=logger,
    )

    assert result == {}


def test_load_last_plan_reads_json_when_present(tmp_path):
    plan_path = tmp_path / ".codingAgent" / "last_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text('{"task": "x", "plan": [1]}', encoding="utf-8")
    logger = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()

    result = load_last_plan(
        workdir=str(tmp_path),
        get_last_plan_path_fn=lambda workdir: plan_path,
        logger=logger,
    )

    assert result == {"task": "x", "plan": [1]}


def test_save_last_plan_persists_json_via_atomic_write(tmp_path):
    calls = []
    logger = type(
        "L",
        (),
        {"debug": lambda *a, **k: calls.append(("debug", a[1] if len(a) > 1 else a[0])), "info": lambda *a, **k: calls.append(("info", a[1] if len(a) > 1 else a[0])), "warning": lambda *a, **k: calls.append(("warning", a[1] if len(a) > 1 else a[0]))},
    )()
    plan_path = tmp_path / ".codingAgent" / "last_plan.json"

    def _atomic_write_json(path, data, logger=None):
        path.write_text(__import__("json").dumps(data), encoding="utf-8")
        return True

    save_last_plan(
        workdir=str(tmp_path),
        plan=[{"description": "step"}],
        task="task",
        step=1,
        get_last_plan_path_fn=lambda workdir: plan_path,
        logger=logger,
        atomic_write_json_fn=_atomic_write_json,
        now_fn=lambda: datetime(2024, 1, 2, 3, 4, 5),
    )

    assert plan_path.exists()
    saved = __import__("json").loads(plan_path.read_text(encoding="utf-8"))
    assert saved["current_step"] == 1
    assert saved["saved_at"] == "2024-01-02T03:04:05"


def test_save_last_plan_falls_back_when_atomic_write_returns_false(tmp_path):
    logger = type("L", (), {"debug": lambda *a, **k: None, "info": lambda *a, **k: None, "warning": lambda *a, **k: None})()
    plan_path = tmp_path / ".codingAgent" / "last_plan.json"

    save_last_plan(
        workdir=str(tmp_path),
        plan=[{"description": "step"}],
        task="task",
        step=0,
        get_last_plan_path_fn=lambda workdir: plan_path,
        logger=logger,
        atomic_write_json_fn=lambda path, data, logger=None: False,
        now_fn=lambda: datetime(2024, 1, 2, 3, 4, 5),
    )

    assert plan_path.exists()
