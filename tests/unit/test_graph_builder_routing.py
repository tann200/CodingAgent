import pytest
from typing import Any
from src.core.orchestration.graph.builder import (
    should_after_execution_with_replan,
    should_after_planning,
    should_after_replan,
    should_after_evaluation,
    route_after_perception,
)
from src.core.orchestration.graph.state import AgentState


def _make_state(**kwargs: Any) -> AgentState:
    """Helper to create AgentState with all required fields."""
    default: AgentState = {
        "task": "",
        "history": [],
        "verified_reads": [],
        "rounds": 0,
        "working_dir": ".",
        "system_prompt": "",
        "next_action": None,
        "last_result": None,
        "errors": [],
        "current_plan": None,
        "current_step": 0,
        "deterministic": False,
        "seed": None,
        "analysis_summary": None,
        "relevant_files": None,
        "key_symbols": None,
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": True,
        "task_decomposed": False,
        "tool_last_used": None,
        "tool_call_count": 0,
        "max_tool_calls": 50,
        "files_read": None,
        "repo_summary_data": None,
        "replan_required": None,
        "action_failed": False,
        "plan_progress": None,
        "evaluation_result": None,
        "cancel_event": None,
        "empty_response_count": 0,
    }
    for key, value in kwargs.items():
        if key in default:
            default[key] = value
    return default


def test_should_after_planning_with_plan():
    """Test routing after planning when a plan exists."""
    state = _make_state(
        rounds=1,
        next_action=None,
        current_plan=[{"description": "step 1"}],
        last_result=None,
    )
    result = should_after_planning(state)
    assert result == "execute"


def test_should_after_planning_with_next_action():
    """Test routing after planning when a next_action exists."""
    state = _make_state(
        rounds=1,
        next_action={"name": "echo"},
        current_plan=[],
        last_result=None,
    )
    result = should_after_planning(state)
    assert result == "execute"


def test_should_after_planning_with_last_result():
    """Test routing after planning when last_result exists."""
    state = _make_state(
        rounds=5,
        next_action=None,
        current_plan=[],
        last_result={"ok": True},
    )
    result = should_after_planning(state)
    assert result == "memory_sync"


def test_should_after_execution_with_replan():
    """Test routing after execution when replan is required."""
    state = _make_state(
        current_plan=[{"description": "step 1"}],
        current_step=0,
        last_result={"ok": True},
        replan_required="Patch exceeded 200 lines. Split into multiple targeted functions.",
    )
    result = should_after_execution_with_replan(state)
    assert result == "replan"


def test_should_after_execution_with_more_steps():
    """Test routing after execution when more steps remain."""
    state = _make_state(
        current_plan=[{"description": "step 1"}, {"description": "step 2"}],
        current_step=0,
        last_result={"ok": True},
        replan_required=None,
    )
    result = should_after_execution_with_replan(state)
    assert result == "execution"


def test_should_after_execution_no_more_steps():
    """Test routing after execution when no more steps remain."""
    state = _make_state(
        current_plan=[{"description": "step 1"}, {"description": "step 2"}],
        current_step=1,
        last_result={"ok": True},
        replan_required=None,
        step_controller_enabled=False,
    )
    result = should_after_execution_with_replan(state)
    assert result == "verification"


def test_should_after_execution_empty_plan():
    """Test routing after execution with empty plan."""
    state = _make_state(
        current_plan=[],
        current_step=0,
        last_result=None,
        replan_required=None,
    )
    result = should_after_execution_with_replan(state)
    assert result == "perception"


def test_should_after_replan_success():
    """Test routing after replan when replan succeeded."""
    state = _make_state(
        replan_required=None,
    )
    result = should_after_replan(state)
    assert result == "step_controller"


def test_should_after_replan_still_required():
    """Test routing after replan when replan is still needed."""
    state = _make_state(
        replan_required="Failed to split step",
    )
    result = should_after_replan(state)
    assert result == "perception"


def test_should_after_evaluation_complete():
    """Test routing after evaluation when task is complete."""
    state = _make_state(
        evaluation_result="complete",
    )
    result = should_after_evaluation(state)
    assert result == "memory_sync"


def test_should_after_evaluation_replan():
    """Test routing after evaluation when more work is needed."""
    state = _make_state(
        evaluation_result="replan",
    )
    result = should_after_evaluation(state)
    assert result == "step_controller"


def test_should_after_evaluation_end():
    """Test routing after evaluation when task should end."""
    state = _make_state(
        evaluation_result="end",
    )
    result = should_after_evaluation(state)
    assert result == "end"


def test_route_after_perception_fast_path():
    """Test fast-path routing when next_action exists."""
    state = _make_state(
        next_action={"name": "read_file", "arguments": {"path": "test.py"}},
    )
    result = route_after_perception(state)
    assert result == "execution"


def test_route_after_perception_standard_path():
    """Test standard routing when no next_action."""
    state = _make_state(
        next_action=None,
    )
    result = route_after_perception(state)
    assert result == "analysis"
