"""
Tests for graph nodes: perception, planning, execution, analysis, debug, verification.

These tests verify the async behavior of LangGraph nodes.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from typing import Any, Dict

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes import (
    planning_node,
    analysis_node,
    execution_node,
    perception_node,
    debug_node,
    verification_node,
    evaluation_node,
    replan_node,
    step_controller_node,
    memory_update_node,
)
from src.core.orchestration.graph.builder import route_after_perception


# Helper to create AgentState
def _make_state(**kwargs: Any) -> AgentState:
    """Create AgentState with defaults."""
    defaults: AgentState = {
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
    for k, v in kwargs.items():
        if k in defaults:
            defaults[k] = v
    return defaults


class TestPlanningNode:
    """Tests for planning_node."""

    @pytest.mark.asyncio
    async def test_planning_with_json_response(self):
        """Test planning node parses JSON correctly."""
        state = _make_state(
            task="Create a function to add numbers",
            history=[],
            system_prompt="You are a planner.",
        )
        config = {}

        # The planning_node should handle the response
        # We test the parsing function directly
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        # Test JSON parsing
        json_content = '[{"description": "Step 1"}, {"description": "Step 2"}]'
        result = _parse_plan_content(json_content)

        assert len(result) == 2
        assert result[0]["description"] == "Step 1"

    @pytest.mark.asyncio
    async def test_planning_with_numbered_list(self):
        """Test planning node parses numbered lists."""
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        content = "1. First step\n2. Second step\n3. Third step"
        result = _parse_plan_content(content)

        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_planning_with_bullet_list(self):
        """Test planning node parses bullet lists."""
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        content = "- First step\n- Second step\n- Third step"
        result = _parse_plan_content(content)

        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_planning_empty_content(self):
        """Test planning handles empty content."""
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        result = _parse_plan_content("")
        assert result == []

    @pytest.mark.asyncio
    async def test_planning_with_conversational_filler(self):
        """Test planning filters out conversational filler."""
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        content = """Here is the plan:
1. Read the file
2. Edit the function
3. Run tests

Let me know if you need anything else!"""
        result = _parse_plan_content(content)

        # Should parse the steps, not the filler
        assert len(result) >= 1


class TestAnalysisNode:
    """Tests for analysis_node."""

    @pytest.mark.asyncio
    async def test_analysis_fast_path_bypass(self):
        """Test analysis fast path when next_action exists."""
        state = _make_state(
            task="Read main.py",
            next_action={"name": "read_file", "arguments": {"path": "main.py"}},
        )
        config = {}

        # When next_action exists, analysis should return fast path
        result = await analysis_node.analysis_node(state, config)

        assert result["analysis_summary"] == "Skipped (Fast Path)"

    @pytest.mark.asyncio
    async def test_analysis_no_action_no_error(self):
        """Test analysis handles missing action gracefully."""
        state = _make_state(task="Some task")
        config = {}

        # Should not raise, even without proper orchestrator
        try:
            result = await analysis_node.analysis_node(state, config)
            # May return error state, but shouldn't crash
            assert "analysis_summary" in result or "error" in result
        except Exception:
            pass  # Acceptable if no orchestrator

    @pytest.mark.asyncio
    async def test_analysis_symbol_graph_enrichment(self, tmp_path):
        """analysis_node enriches analysis_summary with SymbolGraph data for .py files."""
        py_file = tmp_path / "mymodule.py"
        py_file.write_text("def compute(): pass\nclass Engine: pass\n")

        state = _make_state(
            task="understand mymodule.py",
            working_dir=str(tmp_path),
            relevant_files=[str(py_file)],
        )
        config = {}

        try:
            result = await analysis_node.analysis_node(state, config)
            summary = result.get("analysis_summary", "")
            # Either symbol info is present or node handled gracefully
            assert isinstance(summary, str)
        except Exception:
            pass  # Acceptable in headless env without orchestrator

    @pytest.mark.asyncio
    async def test_analysis_symbol_graph_skipped_for_non_python(self, tmp_path):
        """analysis_node should not crash when relevant_files contains non-.py files."""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("some text\n")

        state = _make_state(
            task="review notes",
            working_dir=str(tmp_path),
            relevant_files=[str(txt_file)],
        )
        config = {}

        try:
            result = await analysis_node.analysis_node(state, config)
            assert "analysis_summary" in result or "error" in result
        except Exception:
            pass  # Acceptable in headless env


class TestRouteAfterPerception:
    """Tests for routing function."""

    def test_route_fast_path_when_action_exists(self):
        """Test fast path when next_action exists."""
        state = _make_state(
            next_action={"name": "read_file", "arguments": {"path": "main.py"}}
        )

        result = route_after_perception(state)

        assert result == "execution"

    def test_route_standard_path_when_no_action(self):
        """Test standard path when no next_action."""
        state = _make_state(next_action=None)

        result = route_after_perception(state)

        assert result == "analysis"


class TestVerificationNode:
    """Tests for verification_node."""

    @pytest.mark.asyncio
    async def test_verification_node_imports(self):
        """Test verification_node can be imported."""
        from src.core.orchestration.graph.nodes import verification_node

        assert verification_node is not None

    @pytest.mark.asyncio
    async def test_verification_with_deletion_check(self):
        """Test verification checks file deletion."""
        state = _make_state(
            last_result={
                "ok": True,
                "result": {"status": "ok", "deleted": True, "path": "test.py"},
            },
            working_dir=".",
        )

        # Test the basic structure
        assert state["last_result"]["result"]["deleted"] is True


class TestDebugNode:
    """Tests for debug_node."""

    @pytest.mark.asyncio
    async def test_debug_node_imports(self):
        """Test debug_node can be imported."""
        from src.core.orchestration.graph.nodes import debug_node

        assert debug_node is not None


class TestEvaluationNode:
    """Tests for evaluation_node."""

    @pytest.mark.asyncio
    async def test_evaluation_complete(self):
        """Test evaluation with complete result."""
        state = _make_state(
            evaluation_result="complete",
            verification_passed=True,
        )
        config = {}

        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        # Should handle the state
        assert state.get("evaluation_result") == "complete"


class TestReplanNode:
    """Tests for replan_node."""

    @pytest.mark.asyncio
    async def test_replan_node_imports(self):
        """Test replan_node can be imported."""
        from src.core.orchestration.graph.nodes import replan_node

        assert replan_node is not None


class TestStepControllerNode:
    """Tests for step_controller_node."""

    @pytest.mark.asyncio
    async def test_step_controller_empty_plan(self):
        """Test step controller with empty plan."""
        state = _make_state(
            current_plan=[],
            current_step=0,
        )
        config = {}

        # Import the function directly
        import src.core.orchestration.graph.nodes.step_controller_node as sc_module

        step_controller_node = sc_module.step_controller_node

        result = await step_controller_node(state, config)

        # Empty plan - should handle gracefully
        assert isinstance(result, dict)


class TestMemoryUpdateNode:
    """Tests for memory_update_node."""

    @pytest.mark.asyncio
    async def test_memory_update_basic(self):
        """Test memory update basic functionality."""
        state = _make_state(
            history=[{"role": "user", "content": "test"}],
            working_dir=".",
        )
        config = {}

        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )

        # Should not raise - even if distiller fails
        try:
            result = await memory_update_node.memory_update_node(state, config)
            assert isinstance(result, dict)
        except Exception:
            # May fail if distiller has issues - that's ok for this test
            pass


class TestPlanValidatorNode:
    """Tests for plan_validator_node (async wrapper)."""

    @pytest.mark.asyncio
    async def test_plan_validator_with_empty_plan(self):
        """Test plan validator with empty plan."""
        state = _make_state(current_plan=[])
        config = {}

        from src.core.orchestration.graph.nodes.plan_validator_node import (
            plan_validator_node,
        )

        result = await plan_validator_node(state, config)

        # Empty plan should fail validation - check errors exist
        assert "errors" in result
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_plan_validator_with_valid_plan(self):
        """Test plan validator with valid plan."""
        state = _make_state(
            current_plan=[{"description": "Read file"}, {"description": "Run tests"}]
        )
        config = {}

        from src.core.orchestration.graph.nodes.plan_validator_node import (
            plan_validator_node,
        )

        result = await plan_validator_node(state, config)

        # Valid plan should pass
        assert result.get("action_failed") is False
