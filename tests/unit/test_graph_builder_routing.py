from typing import Any
from src.core.orchestration.graph.builder import (
    should_after_execution_with_replan,
    should_after_planning,
    should_after_replan,
    should_after_evaluation,
    route_after_perception,
    should_after_analysis,
    _task_is_complex,
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
        "analyst_findings": None,
        "plan_resumed": None,
        "last_debug_error_type": None,
        "session_id": None,
        "delegation_results": None,
        "delegations": None,
        "last_tool_name": None,
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
    """W5: execution routes to step_controller (not execution) when more plan steps remain."""
    state = _make_state(
        current_plan=[{"description": "step 1"}, {"description": "step 2"}],
        current_step=0,
        last_result={"ok": True},
        replan_required=None,
    )
    result = should_after_execution_with_replan(state)
    assert result == "step_controller"


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
    """Test fast-path routing when next_action exists and task is simple."""
    state = _make_state(
        task="read the file",
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


# ---------------------------------------------------------------------------
# W3: Fast-path complexity guard
# ---------------------------------------------------------------------------

def test_task_is_complex_keyword_refactor():
    """W3: 'refactor' keyword marks task as complex."""
    state = _make_state(task="refactor the authentication module")
    assert _task_is_complex(state) is True


def test_task_is_complex_keyword_implement():
    """W3: 'implement' keyword marks task as complex."""
    state = _make_state(task="implement user authentication with JWT")
    assert _task_is_complex(state) is True


def test_task_is_complex_many_relevant_files():
    """W3: more than 3 relevant_files marks task as complex."""
    state = _make_state(relevant_files=["a.py", "b.py", "c.py", "d.py"])
    assert _task_is_complex(state) is True


def test_task_is_complex_existing_plan():
    """W3: existing plan with 2+ steps marks task as complex."""
    state = _make_state(current_plan=[{"description": "s1"}, {"description": "s2"}])
    assert _task_is_complex(state) is True


def test_task_is_not_complex_simple():
    """W3: simple one-line task is not complex."""
    state = _make_state(task="show me the contents of main.py")
    assert _task_is_complex(state) is False


def test_route_after_perception_complex_task_overrides_fast_path():
    """W3: complex task overrides fast-path even when next_action is set."""
    state = _make_state(
        task="refactor the entire authentication system across multiple files",
        next_action={"name": "edit_file_atomic", "arguments": {}},
    )
    result = route_after_perception(state)
    assert result == "analysis"


def test_route_after_perception_simple_task_fast_path_ok():
    """W3: simple task still uses fast-path when next_action is set."""
    state = _make_state(
        task="show me the file",
        next_action={"name": "read_file", "arguments": {"path": "test.py"}},
    )
    result = route_after_perception(state)
    assert result == "execution"


# ---------------------------------------------------------------------------
# W12: Tool call budget enforcement
# ---------------------------------------------------------------------------

def test_budget_exhausted_routes_to_memory_sync():
    """W12: when tool_call_count >= max_tool_calls, route to memory_sync."""
    state = _make_state(
        tool_call_count=30,
        max_tool_calls=30,
        current_plan=[{"description": "step 1"}],
        current_step=0,
        last_result={"ok": True},
    )
    result = should_after_execution_with_replan(state)
    assert result == "memory_sync"


def test_budget_not_exhausted_routes_normally():
    """W12: below budget limit, routing proceeds normally."""
    state = _make_state(
        tool_call_count=5,
        max_tool_calls=30,
        current_plan=[],
        current_step=0,
        last_result={"ok": True},
        replan_required=None,
    )
    # no plan + success → memory_sync via normal routing, not budget
    result = should_after_execution_with_replan(state)
    assert result == "memory_sync"


def test_budget_one_over_limit_routes_to_memory_sync():
    """W12: one call over budget still bails to memory_sync."""
    state = _make_state(
        tool_call_count=31,
        max_tool_calls=30,
        current_plan=[{"description": "more steps"}],
        current_step=0,
        last_result={"ok": True},
    )
    result = should_after_execution_with_replan(state)
    assert result == "memory_sync"


def test_budget_zero_max_falls_back_to_default():
    """W12: max_tool_calls=0 is falsy — falls back to default of 30, so budget not exhausted."""
    state = _make_state(
        tool_call_count=1,
        max_tool_calls=0,  # falsy → treated as 30 via `or 30` fallback
        current_plan=[{"description": "step"}],
        current_step=0,
        last_result={"ok": True},
    )
    # tool_call_count=1 < 30 (default) → not exhausted → normal routing
    result = should_after_execution_with_replan(state)
    assert result != "memory_sync"  # budget not hit, normal path taken


# ---------------------------------------------------------------------------
# #56: should_after_analysis routing
# ---------------------------------------------------------------------------

def test_should_after_analysis_simple_task_goes_to_planning():
    """#56: simple task routes directly to planning."""
    state = _make_state(task="read a file")
    assert should_after_analysis(state) == "planning"


def test_should_after_analysis_complex_keyword_goes_to_analyst():
    """#56: complex keyword routes to analyst_delegation."""
    state = _make_state(task="refactor the authentication system")
    assert should_after_analysis(state) == "analyst_delegation"


def test_should_after_analysis_many_files_goes_to_analyst():
    """#56: >3 relevant files routes to analyst_delegation."""
    state = _make_state(
        task="update config",
        relevant_files=["a.py", "b.py", "c.py", "d.py"],
    )
    assert should_after_analysis(state) == "analyst_delegation"
