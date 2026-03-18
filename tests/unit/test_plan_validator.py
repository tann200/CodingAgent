"""Tests for plan_validator_node."""

import pytest
from src.core.orchestration.graph.nodes.plan_validator_node import (
    validate_plan,
    plan_validator_node,
)
from src.core.orchestration.graph.state import AgentState


class TestValidatePlan:
    """Tests for validate_plan function."""

    def test_valid_plan_with_verification(self):
        """Test that a valid plan with verification passes."""
        plan = [
            {"description": "Read the main.py file"},
            {"description": "Modify the function"},
            {"description": "Run tests to verify"},
        ]
        result = validate_plan(plan)

        assert result["valid"] is True
        assert len(result["errors"]) == 0
        assert result["has_verification"] is True

    def test_valid_plan_no_verification_warning(self):
        """Test that a plan without verification gets a warning."""
        plan = [
            {"description": "Read the main.py file"},
            {"description": "Modify the function"},
        ]
        result = validate_plan(plan)

        assert result["valid"] is True
        assert len(result["errors"]) == 0
        assert result["has_verification"] is False
        assert "verification" in result["warnings"][0].lower()

    def test_empty_plan(self):
        """Test that an empty plan is invalid."""
        result = validate_plan([])

        assert result["valid"] is False
        assert (
            "empty" in result["errors"][0].lower()
            or "no" in result["errors"][0].lower()
        )

    def test_none_plan(self):
        """Test that None plan is invalid."""
        result = validate_plan(None)

        assert result["valid"] is False

    def test_plan_references_files(self):
        """Test that plan with file references is detected."""
        plan = [
            {"description": "Update imports in main.py"},
            {"description": "Add function to utils.py"},
        ]
        result = validate_plan(plan)

        assert result["steps_with_files"] == 2

    def test_plan_too_many_steps_warning(self):
        """Test that very long plans get a warning."""
        plan = [{"description": f"Step {i}"} for i in range(25)]
        result = validate_plan(plan)

        assert len(result["warnings"]) > 0


class TestPlanValidatorNode:
    """Tests for plan_validator_node async function."""

    @pytest.mark.asyncio
    async def test_valid_plan_passes(self):
        """Test that a valid plan passes validation."""
        state: AgentState = {
            "task": "test",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": [
                {"description": "Read file"},
                {"description": "Run tests"},
            ],
            "current_step": 0,
            "deterministic": None,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": None,
            "key_symbols": None,
            "debug_attempts": None,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "verification_result": None,
            "step_controller_enabled": False,
            "task_decomposed": None,
            "tool_last_used": None,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "files_read": None,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": None,
            "plan_progress": None,
            "evaluation_result": None,
            "cancel_event": None,
        }

        config = None
        result = await plan_validator_node(state, config)

        assert result["action_failed"] is False
        assert result["plan_validation"]["valid"] is True

    @pytest.mark.asyncio
    async def test_invalid_plan_fails(self):
        """Test that an invalid plan fails validation."""
        state: AgentState = {
            "task": "test",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": [],  # Empty plan
            "current_step": 0,
            "deterministic": None,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": None,
            "key_symbols": None,
            "debug_attempts": None,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "verification_result": None,
            "step_controller_enabled": False,
            "task_decomposed": None,
            "tool_last_used": None,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "files_read": None,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": None,
            "plan_progress": None,
            "evaluation_result": None,
            "cancel_event": None,
        }

        config = None
        result = await plan_validator_node(state, config)

        assert "errors" in result
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_no_plan_returns_error(self):
        """Test that missing plan returns error."""
        state: AgentState = {
            "task": "test",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": None,
            "current_step": 0,
            "deterministic": None,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": None,
            "key_symbols": None,
            "debug_attempts": None,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "verification_result": None,
            "step_controller_enabled": False,
            "task_decomposed": None,
            "tool_last_used": None,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "files_read": None,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": None,
            "plan_progress": None,
            "evaluation_result": None,
            "cancel_event": None,
        }

        config = None
        result = await plan_validator_node(state, config)

        assert "errors" in result
        assert len(result["errors"]) > 0
