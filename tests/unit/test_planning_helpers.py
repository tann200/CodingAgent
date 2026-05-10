from src.core.orchestration.graph.nodes.planning_helpers import (
    resolve_planning_orchestrator,
)


def test_resolve_planning_orchestrator_returns_orchestrator_when_found():
    logger = type("L", (), {"error": lambda *a, **k: None})()
    orchestrator = object()

    result, error = resolve_planning_orchestrator(
        state={},
        config={},
        plan_attempts=2,
        resolve_orchestrator_fn=lambda state, config: orchestrator,
        build_planning_error_result_fn=lambda **kwargs: kwargs,
        logger=logger,
    )

    assert result is orchestrator
    assert error is None


def test_resolve_planning_orchestrator_returns_missing_orchestrator_error():
    logged = []
    logger = type(
        "L",
        (),
        {"error": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    result, error = resolve_planning_orchestrator(
        state={"current_plan": [{"description": "x"}], "current_step": 1},
        config={},
        plan_attempts=3,
        resolve_orchestrator_fn=lambda state, config: None,
        build_planning_error_result_fn=lambda **kwargs: kwargs,
        logger=logger,
    )

    assert result is None
    assert error == {
        "current_plan": [{"description": "x"}],
        "current_step": 1,
        "plan_attempts": 3,
        "errors": ["orchestrator not found"],
    }
    assert logged == ["planning_node: orchestrator is None"]


def test_resolve_planning_orchestrator_returns_config_error_on_exception():
    logged = []
    logger = type(
        "L",
        (),
        {"error": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    def _raise(state, config):
        raise RuntimeError("boom")

    result, error = resolve_planning_orchestrator(
        state={"current_plan": [], "current_step": 0},
        config={},
        plan_attempts=4,
        resolve_orchestrator_fn=_raise,
        build_planning_error_result_fn=lambda **kwargs: kwargs,
        logger=logger,
    )

    assert result is None
    assert error == {
        "current_plan": [],
        "current_step": 0,
        "plan_attempts": 4,
        "errors": ["config error: boom"],
    }
    assert logged == ["planning_node: failed to get orchestrator: %s"]
