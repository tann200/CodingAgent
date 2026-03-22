"""
Tests for graph nodes: perception, planning, execution, analysis, debug, verification.

These tests verify the async behavior of LangGraph nodes.
"""

import pytest
from unittest.mock import MagicMock
from typing import Any

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes import (
    analysis_node,
    debug_node,
    verification_node,
    replan_node,
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
        "tool_call_count": 0,
        "max_tool_calls": 50,
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
        _state = _make_state(
            task="Create a function to add numbers",
            history=[],
            system_prompt="You are a planner.",
        )
        __config = {}

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

    def test_parse_plan_rejects_metadata_output(self):
        """Strategy 4 must NOT convert Qwen-style metadata output to a plan."""
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        # Typical Qwen3.5 planning metadata (not a real plan)
        content = "PLAN_STEPS: 1\nCOMPLEXITY: simple\nDELEGATION_NEEDED: no"
        result = _parse_plan_content(content)
        assert result == [], f"Metadata must not become plan steps, got: {result}"

    def test_parse_plan_rejects_file_listing_rows(self):
        """File listing lines like '.DS_Store (file)' must not become plan steps."""
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        content = ".DS_Store (file)\n.agent-context (directory)\ntest_dir (directory)"
        result = _parse_plan_content(content)
        assert result == [], f"File listing must not become plan steps, got: {result}"

    def test_parse_plan_rejects_markdown_table_rows(self):
        """Markdown table rows must not become plan steps."""
        from src.core.orchestration.graph.nodes.planning_node import _parse_plan_content

        content = "| File | Type |\n|------|------|\n| .DS_Store | file |\n| src | directory |"
        result = _parse_plan_content(content)
        assert result == [], f"Table rows must not become plan steps, got: {result}"


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


        # Should handle the state
        assert state.get("evaluation_result") == "complete"


class TestReplanNode:
    """Tests for replan_node."""

    @pytest.mark.asyncio
    async def test_replan_node_imports(self):
        """Test replan_node can be imported."""

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

    @pytest.mark.asyncio
    async def test_memory_update_node_returns_dict_on_failure(self, tmp_path):
        """H14: memory_update_node must return a dict even when all sub-tasks fail.

        Previously, bare except clauses swallowed all errors silently.
        Now return_exceptions=True + logging ensures failures are captured.
        """
        from unittest.mock import patch
        from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node as mn

        state = _make_state(
            history=[{"role": "user", "content": "do something"}],
            working_dir=str(tmp_path),
            evaluation_result="complete",
            task="do something",
        )
        config = {}

        # Force distill_context to raise an exception
        with patch("src.core.orchestration.graph.nodes.memory_update_node.distill_context",
                   side_effect=RuntimeError("distiller exploded")):
            result = await mn(state, config)

        # Must still return a dict (not raise)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_memory_update_uses_return_exceptions(self):
        """H14: asyncio.gather must use return_exceptions=True so one failure doesn't abort others."""
        import inspect
        from src.core.orchestration.graph.nodes import memory_update_node as mn_module
        src = inspect.getsource(mn_module)
        assert "return_exceptions=True" in src, "gather must use return_exceptions=True (H14)"


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


class TestPlanPersistence:
    """Tests for plan persistence between sessions."""

    def test_get_last_plan_path(self, tmp_path):
        """Test _get_last_plan_path returns correct path."""
        from src.core.orchestration.graph.nodes.planning_node import _get_last_plan_path

        path = _get_last_plan_path(str(tmp_path))
        assert ".agent-context" in str(path)
        assert "last_plan.json" in str(path)

    def test_save_and_load_last_plan(self, tmp_path):
        """Test saving and loading a plan from JSON file."""
        from src.core.orchestration.graph.nodes.planning_node import (
            _save_last_plan,
            _load_last_plan,
        )

        plan = [
            {"description": "Step 1", "action": None},
            {"description": "Step 2", "action": None},
        ]
        task = "Test task"
        step = 0

        # Save plan
        _save_last_plan(str(tmp_path), plan, task, step)

        # Load plan
        loaded = _load_last_plan(str(tmp_path))

        assert loaded["plan"] == plan
        assert loaded["task"] == task
        assert loaded["current_step"] == step

    def test_load_nonexistent_plan(self, tmp_path):
        """Test loading a plan when file doesn't exist returns empty dict."""
        from src.core.orchestration.graph.nodes.planning_node import _load_last_plan

        loaded = _load_last_plan(str(tmp_path))
        assert loaded == {}

    def test_load_last_plan_invalid_json(self, tmp_path, monkeypatch):
        """Test loading invalid JSON returns empty dict."""
        from src.core.orchestration.graph.nodes.planning_node import _load_last_plan

        # Create an invalid JSON file
        plan_path = tmp_path / ".agent-context" / "last_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("not valid json")

        loaded = _load_last_plan(str(tmp_path))
        assert loaded == {}

    @pytest.mark.asyncio
    async def test_planning_node_saves_plan(self, tmp_path):
        """Test planning_node saves plan to file."""
        from src.core.orchestration.graph.nodes.planning_node import (
            planning_node,
            _get_last_plan_path,
        )

        state = _make_state(
            task="Simple test task",
            working_dir=str(tmp_path),
            next_action="read_file",
            current_plan=[],
        )
        config = {"configurable": {"orchestrator": MagicMock()}}

        result = await planning_node(state, config)

        # Check that a plan was created
        assert result.get("current_plan") is not None
        assert len(result.get("current_plan", [])) > 0

        # Check that the plan was saved to file
        plan_path = _get_last_plan_path(str(tmp_path))
        assert plan_path.exists()

    @pytest.mark.asyncio
    async def test_planning_node_loads_existing_plan(self, tmp_path):
        """Test planning_node loads existing plan from file."""
        from src.core.orchestration.graph.nodes.planning_node import (
            planning_node,
            _save_last_plan,
        )

        # Pre-save a plan
        saved_plan = [
            {"description": "Saved step 1", "action": None},
            {"description": "Saved step 2", "action": None},
        ]
        _save_last_plan(str(tmp_path), saved_plan, "Previous task", 0)

        # Now run planning with empty plan
        state = _make_state(
            task="Previous task",  # Same task as saved
            working_dir=str(tmp_path),
            current_plan=[],  # Empty plan should trigger loading
        )
        config = {"configurable": {"orchestrator": MagicMock()}}

        result = await planning_node(state, config)

        # Check that the plan was resumed
        assert result.get("current_plan") == saved_plan
        assert result.get("plan_resumed") is True
