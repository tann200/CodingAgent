def test_build_planning_error_result_sets_reset_fields():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_planning_error_result,
    )

    result = _build_planning_error_result(
        current_plan=[{"description": "x"}],
        current_step=2,
        plan_attempts=4,
        errors=["orchestrator not found"],
    )

    assert result == {
        "current_plan": [{"description": "x"}],
        "current_step": 2,
        "plan_attempts": 4,
        "plan_mode_approved": None,
        "errors": ["orchestrator not found"],
    }


def test_build_resumed_plan_result_marks_plan_resumed():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_resumed_plan_result,
    )

    plan = [{"description": "saved step"}]
    result = _build_resumed_plan_result(
        loaded_plan=plan,
        loaded_step=1,
        plan_attempts=2,
    )

    assert result["current_plan"] == plan
    assert result["current_step"] == 1
    assert result["task_decomposed"] is True
    assert result["plan_resumed"] is True
    assert result["plan_attempts"] == 2
    assert result["plan_mode_approved"] is None


def test_build_existing_plan_result_preserves_step_description():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_existing_plan_result,
    )

    plan = [{"description": "step 0"}, {"description": "step 1"}]
    result = _build_existing_plan_result(
        current_plan=plan,
        current_step=0,
        step_description="step 0",
        plan_attempts=1,
    )

    assert result["current_plan"] == plan
    assert result["current_step"] == 0
    assert result["step_description"] == "step 0"
    assert result["task_decomposed"] is True
    assert result["plan_attempts"] == 1
    assert result["plan_mode_approved"] is None


def test_build_simple_next_action_plan_result_sets_basic_fields():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_simple_next_action_plan_result,
    )

    result = _build_simple_next_action_plan_result(
        current_plan=[{"description": "Execute the requested tool", "action": {"name": "read_file"}}],
        current_step=0,
        plan_attempts=2,
    )

    assert result == {
        "current_plan": [{"description": "Execute the requested tool", "action": {"name": "read_file"}}],
        "current_step": 0,
        "plan_attempts": 2,
        "plan_mode_approved": None,
    }


def test_build_planning_early_response_result_includes_optional_next_action():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_planning_early_response_result,
    )

    result = _build_planning_early_response_result(
        current_plan=[],
        current_step=0,
        plan_attempts=3,
        early_resp={
            "errors": ["canceled"],
            "next_action": {"name": "respond", "arguments": {"message": "stopped"}},
        },
    )

    assert result["current_plan"] == []
    assert result["current_step"] == 0
    assert result["plan_attempts"] == 3
    assert result["plan_mode_approved"] is None
    assert result["errors"] == ["canceled"]
    assert result["next_action"] == {"name": "respond", "arguments": {"message": "stopped"}}


def test_build_planning_early_response_result_omits_missing_next_action():
    from src.core.orchestration.graph.nodes.planning_node import (
        _build_planning_early_response_result,
    )

    result = _build_planning_early_response_result(
        current_plan=[],
        current_step=1,
        plan_attempts=4,
        early_resp={"errors": []},
    )

    assert result["errors"] == []
    assert "next_action" not in result
