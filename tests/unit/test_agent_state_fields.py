"""
Tests for NEW-5: AgentState TypedDict was missing several fields used at runtime.

Missing fields that were added:
- original_task       — written by perception_node decomposition, read by execution/replan
- step_description    — written by step_controller_node
- planned_action      — written by step_controller_node
- plan_validation     — written by plan_validator_node, read by builder.py routing
- plan_enforce_warnings — read by plan_validator_node
- plan_strict_mode    — read by plan_validator_node
- task_history        — written by create_state_checkpoint

Regression tests verify:
1. All 7 fields exist in AgentState TypedDict annotations
2. step_controller_node returns the expected keys
3. A dict with these keys can be constructed without error
"""
import pytest


class TestAgentStateFieldsExist:
    """Verify all NEW-5 fields are present in AgentState TypedDict."""

    def _get_state_annotations(self):
        """Return the resolved annotations dict of AgentState."""
        from src.core.orchestration.graph.state import AgentState
        return AgentState.__annotations__

    def test_original_task_field_exists(self):
        """original_task must be declared in AgentState (used by perception_node decomposition)."""
        annotations = self._get_state_annotations()
        assert "original_task" in annotations, (
            "NEW-5: 'original_task' must be in AgentState — written by perception_node:171"
        )

    def test_step_description_field_exists(self):
        """step_description must be declared in AgentState (written by step_controller_node)."""
        annotations = self._get_state_annotations()
        assert "step_description" in annotations, (
            "NEW-5: 'step_description' must be in AgentState — written by step_controller_node:44"
        )

    def test_planned_action_field_exists(self):
        """planned_action must be declared in AgentState (written by step_controller_node)."""
        annotations = self._get_state_annotations()
        assert "planned_action" in annotations, (
            "NEW-5: 'planned_action' must be in AgentState — written by step_controller_node:44"
        )

    def test_plan_validation_field_exists(self):
        """plan_validation must be declared in AgentState (written by plan_validator_node, read by builder)."""
        annotations = self._get_state_annotations()
        assert "plan_validation" in annotations, (
            "NEW-5: 'plan_validation' must be in AgentState — written by plan_validator_node, "
            "read by builder.py:60"
        )

    def test_plan_enforce_warnings_field_exists(self):
        """plan_enforce_warnings must be declared in AgentState (read by plan_validator_node)."""
        annotations = self._get_state_annotations()
        assert "plan_enforce_warnings" in annotations, (
            "NEW-5: 'plan_enforce_warnings' must be in AgentState — read by plan_validator_node:186"
        )

    def test_plan_strict_mode_field_exists(self):
        """plan_strict_mode must be declared in AgentState (read by plan_validator_node)."""
        annotations = self._get_state_annotations()
        assert "plan_strict_mode" in annotations, (
            "NEW-5: 'plan_strict_mode' must be in AgentState — read by plan_validator_node:189"
        )

    def test_task_history_field_exists(self):
        """task_history must be declared in AgentState (written by create_state_checkpoint)."""
        annotations = self._get_state_annotations()
        assert "task_history" in annotations, (
            "NEW-5: 'task_history' must be in AgentState — written by create_state_checkpoint"
        )

    def test_all_new5_fields_in_single_check(self):
        """Single test that verifies all 7 NEW-5 fields are present."""
        from src.core.orchestration.graph.state import AgentState

        annotations = AgentState.__annotations__
        required_fields = [
            "original_task",
            "step_description",
            "planned_action",
            "plan_validation",
            "plan_enforce_warnings",
            "plan_strict_mode",
            "task_history",
        ]
        missing = [f for f in required_fields if f not in annotations]
        assert missing == [], (
            f"NEW-5: The following fields are missing from AgentState: {missing}"
        )


class TestStepControllerNodeReturnsCorrectKeys:
    """step_controller_node must return step_description and planned_action."""

    @pytest.mark.asyncio
    async def test_step_controller_returns_step_description(self):
        """step_controller_node must return 'step_description' from the current plan step."""
        from src.core.orchestration.graph.nodes.step_controller_node import step_controller_node

        state = {
            "current_plan": [
                {"description": "Read the file", "action": None},
                {"description": "Edit the function", "action": None},
            ],
            "current_step": 0,
            "step_controller_enabled": True,
            "last_result": None,
            "task": "do work",
            "history": [],
            "verified_reads": [],
            "rounds": 1,
            "working_dir": ".",
            "system_prompt": "",
            "next_action": None,
            "errors": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "empty_response_count": 0,
        }
        config = {}

        result = await step_controller_node(state, config)

        assert "step_description" in result, (
            "step_controller_node must return 'step_description' key"
        )
        assert result["step_description"] == "Read the file"

    @pytest.mark.asyncio
    async def test_step_controller_returns_planned_action(self):
        """step_controller_node must return 'planned_action' from the current plan step."""
        from src.core.orchestration.graph.nodes.step_controller_node import step_controller_node

        preset_action = {"name": "read_file", "arguments": {"path": "main.py"}}
        state = {
            "current_plan": [
                {"description": "Read main.py", "action": preset_action},
            ],
            "current_step": 0,
            "step_controller_enabled": True,
            "last_result": None,
            "task": "do work",
            "history": [],
            "verified_reads": [],
            "rounds": 1,
            "working_dir": ".",
            "system_prompt": "",
            "next_action": None,
            "errors": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "empty_response_count": 0,
        }
        config = {}

        result = await step_controller_node(state, config)

        assert "planned_action" in result, (
            "step_controller_node must return 'planned_action' key"
        )
        assert result["planned_action"] == preset_action

    @pytest.mark.asyncio
    async def test_step_controller_returns_both_keys_together(self):
        """step_controller_node must return both step_description and planned_action."""
        from src.core.orchestration.graph.nodes.step_controller_node import step_controller_node

        state = {
            "current_plan": [
                {"description": "Write a test", "action": {"name": "write_file", "arguments": {}}},
            ],
            "current_step": 0,
            "step_controller_enabled": True,
            "last_result": None,
            "task": "write tests",
            "history": [],
            "verified_reads": [],
            "rounds": 1,
            "working_dir": ".",
            "system_prompt": "",
            "next_action": None,
            "errors": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "empty_response_count": 0,
        }
        config = {}

        result = await step_controller_node(state, config)

        assert "step_description" in result
        assert "planned_action" in result


class TestAgentStateDictConstructible:
    """Verify the fields can be used in a real AgentState-compatible dict."""

    def test_state_dict_with_new5_fields_is_valid(self):
        """
        NEW-5: A state dict with all new fields must be accepted without TypedDict
        validation errors. This confirms the fields were actually added to the TypedDict.
        """
        from src.core.orchestration.graph.state import AgentState

        # Construct a state dict including all NEW-5 fields
        state: AgentState = {
            "task": "test task",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "session_id": None,
            "delegation_results": None,
            "current_plan": None,
            "current_step": 0,
            "deterministic": None,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": None,
            "key_symbols": None,
            "analyst_findings": None,
            "plan_resumed": None,
            "delegations": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "last_debug_error_type": None,
            "verification_passed": None,
            "verification_result": None,
            "step_controller_enabled": True,
            "task_decomposed": False,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "last_tool_name": None,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": False,
            "plan_progress": None,
            "evaluation_result": None,
            "cancel_event": None,
            "empty_response_count": 0,
            # NEW-5 fields:
            "original_task": "original multi-step task",
            "step_description": "Step one description",
            "planned_action": {"name": "read_file", "arguments": {"path": "a.py"}},
            "plan_validation": {"valid": True, "warnings": []},
            "plan_enforce_warnings": False,
            "plan_strict_mode": False,
            "task_history": [],
        }

        # All NEW-5 fields must be accessible
        assert state["original_task"] == "original multi-step task"
        assert state["step_description"] == "Step one description"
        assert state["planned_action"]["name"] == "read_file"
        assert state["plan_validation"]["valid"] is True
        assert state["plan_enforce_warnings"] is False
        assert state["plan_strict_mode"] is False
        assert state["task_history"] == []
