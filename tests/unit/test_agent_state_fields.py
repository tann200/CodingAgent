"""Tests for slimmed canonical AgentState fields (TASK-25 Stage A)."""

from __future__ import annotations


class TestAgentStateFieldsExist:
    """Verify active canonical fields exist and retired legacy 12-node fields are removed."""

    def _get_state_annotations(self):
        from src.core.orchestration.graph.state import AgentState

        return AgentState.__annotations__

    def test_active_canonical_fields_exist(self):
        annotations = self._get_state_annotations()
        required_fields = [
            "task",
            "original_task",
            "working_dir",
            "history",
            "verified_reads",
            "planned_action",
            "plan_validation",
            "plan_enforce_warnings",
            "task_history",
            "runtime",
            "run",
            "pending_action",
            "last_action",
            "verification",
        ]
        missing = [f for f in required_fields if f not in annotations]
        assert missing == [], f"Active fields missing from AgentState: {missing}"

    def test_legacy_12_node_fields_retired(self):
        annotations = self._get_state_annotations()
        retired_fields = [
            "plan_dag",
            "execution_waves",
            "current_wave",
            "step_description",
            "plan_progress",
            "plan_strict_mode",
            "step_controller_enabled",
            "evaluation_llm_verdict",
            "evaluation_llm_reason",
            "call_graph",
            "test_map",
            "key_symbols",
        ]
        present = [f for f in retired_fields if f in annotations]
        assert present == [], f"Legacy 12-node fields should be retired: {present}"


class TestAgentStateDictConstructible:
    """Verify an active canonical AgentState dict is cleanly constructible."""

    def test_canonical_state_dict_is_valid(self):
        from src.core.orchestration.graph.state import AgentState

        state: AgentState = {  # type: ignore[assignment]
            "task": "test task",
            "original_task": "original multi-step task",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "session_id": None,
            "current_plan": None,
            "current_step": 0,
            "planned_action": {"name": "read_file", "arguments": {"path": "a.py"}},
            "plan_validation": {"valid": True, "warnings": []},
            "plan_enforce_warnings": False,
            "task_history": [],
            "runtime": {},
            "run": {},
            "pending_action": None,
            "last_action": None,
            "verification": None,
        }

        assert state["original_task"] == "original multi-step task"
        assert state["planned_action"]["name"] == "read_file"  # type: ignore[index]
        assert state["plan_validation"]["valid"] is True  # type: ignore[index]
        assert state["plan_enforce_warnings"] is False
        assert state["task_history"] == []
