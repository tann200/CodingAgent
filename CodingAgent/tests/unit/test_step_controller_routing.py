"""Tests for should_after_step_controller routing (P3-T1)."""

from unittest.mock import MagicMock
from langgraph.graph import END

from src.core.orchestration.graph.planning_routing import should_after_step_controller


def test_routes_to_execution_when_plan_active():
    state = {"current_plan": [{"description": "step1"}], "current_step": 0}
    assert should_after_step_controller(state) == "execution"


def test_routes_to_verification_when_plan_exhausted():
    """F-03 fix: plan exhausted must run verification, not jump straight to END."""
    state = {"current_plan": [{"description": "step1"}], "current_step": 1}
    result = should_after_step_controller(state)
    assert result == "verification"


def test_routes_to_planning_when_no_plan():
    state = {"current_plan": [], "current_step": 0}
    assert should_after_step_controller(state) == "planning"


def test_routes_to_end_on_cancellation():
    cancel = MagicMock()
    cancel.is_set.return_value = True
    state = {
        "current_plan": [{"description": "step1"}],
        "current_step": 0,
        "cancel_event": cancel,
    }
    assert should_after_step_controller(state) == "end"


def test_routes_to_verification_when_retries_exhausted():
    state = {
        "current_plan": [{"description": "step1"}],
        "current_step": 0,
        "last_result": {"ok": False, "error": "something failed"},
        "step_retry_counts": {"0": 3},  # at max
    }
    assert should_after_step_controller(state) == "verification"


def test_routes_to_execution_after_successful_step():
    state = {
        "current_plan": [{"description": "step1"}, {"description": "step2"}],
        "current_step": 0,
        "last_result": {"ok": True},
    }
    assert should_after_step_controller(state) == "execution"
