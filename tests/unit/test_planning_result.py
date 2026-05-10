def test_build_resolved_plan_result_includes_standard_fields():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_resolved_plan_result,
    )

    plan = [{"description": "step 1", "files": ["src/a.py"]}]
    waves = [[0]]

    result = _build_resolved_plan_result(
        current_plan=plan,
        current_step=0,
        plan_attempts=3,
        relevant_files=["src/a.py"],
        key_symbols=["Thing"],
        affected_files=["src/a.py"],
        execution_waves=waves,
    )

    assert result["current_plan"] == plan
    assert result["current_step"] == 0
    assert result["task_decomposed"] is True
    assert result["plan_dag"] == {"steps": plan}
    assert result["execution_waves"] == waves
    assert result["current_wave"] == 0
    assert result["plan_attempts"] == 3
    assert result["plan_mode_approved"] is None
    assert result["affected_files"] == ["src/a.py"]
    assert result["relevant_files"] == ["src/a.py"]
    assert result["key_symbols"] == ["Thing"]


def test_build_resolved_plan_result_supports_fallback_shape():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_resolved_plan_result,
    )

    fallback_plan = [{"description": "do the task", "action": None}]

    result = _build_resolved_plan_result(
        current_plan=fallback_plan,
        current_step=0,
        plan_attempts=1,
        relevant_files=[],
        key_symbols=[],
        affected_files=[],
        execution_waves=None,
    )

    assert result["current_plan"] == fallback_plan
    assert result["execution_waves"] is None
    assert result["affected_files"] == []
    assert result["relevant_files"] == []
    assert result["key_symbols"] == []
