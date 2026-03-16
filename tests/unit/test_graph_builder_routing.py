import pytest
from src.core.orchestration.graph.builder import (
    should_after_execution,
    should_after_planning,
)


def test_should_after_planning_with_plan():
    """Test routing after planning when a plan exists."""
    state = {
        "rounds": 1,
        "next_action": None,
        "current_plan": [{"description": "step 1"}],
        "last_result": None,
    }
    result = should_after_planning(state)
    assert result == "execute"


def test_should_after_planning_with_next_action():
    """Test routing after planning when a next_action exists."""
    state = {
        "rounds": 1,
        "next_action": {"name": "echo"},
        "current_plan": [],
        "last_result": None,
    }
    result = should_after_planning(state)
    assert result == "execute"


def test_should_after_planning_with_last_result():
    """Test routing after planning when last_result exists."""
    state = {
        "rounds": 5,
        "next_action": None,
        "current_plan": [],
        "last_result": {"ok": True},
    }
    result = should_after_planning(state)
    assert result == "memory_sync"


def test_should_after_execution_with_more_steps():
    """Test routing after execution when more steps remain."""
    state = {
        "current_plan": [{"description": "step 1"}, {"description": "step 2"}],
        "current_step": 0,
    }
    result = should_after_execution(state)
    assert result == "perception"


def test_should_after_execution_no_more_steps():
    """Test routing after execution when no more steps remain."""
    state = {
        "current_plan": [{"description": "step 1"}, {"description": "step 2"}],
        "current_step": 1,
    }
    result = should_after_execution(state)
    assert result == "verification"


def test_should_after_execution_empty_plan():
    """Test routing after execution with empty plan."""
    state = {
        "current_plan": [],
        "current_step": 0,
    }
    result = should_after_execution(state)
    assert result == "verification"
