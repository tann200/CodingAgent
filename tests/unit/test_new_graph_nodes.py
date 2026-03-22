import pytest
from unittest.mock import MagicMock, patch
from src.core.orchestration.graph.nodes.analysis_node import analysis_node
from src.core.orchestration.graph.nodes.execution_node import execution_node
from src.core.orchestration.graph.nodes.replan_node import replan_node
from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node
from src.core.orchestration.graph.state import AgentState


class TestAnalysisNode:
    """Tests for analysis_node including repo summary generation."""

    @pytest.mark.asyncio
    async def test_analysis_node_generates_repo_summary(self, tmp_path):
        """Test that analysis_node generates repo_summary_data."""
        # Create a simple test file
        (tmp_path / "test.py").write_text("def hello(): pass")

        state: AgentState = {
            "task": "test task",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "working_dir": str(tmp_path),
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
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": None,
        }

        config = MagicMock()

        # Mock orchestrator with tool registry
        mock_orchestrator = MagicMock()
        mock_orchestrator.tool_registry.tools = {}

        with patch(
            "src.core.orchestration.graph.nodes.analysis_node._resolve_orchestrator",
            return_value=mock_orchestrator,
        ):
            result = await analysis_node(state, config)

            assert "repo_summary_data" in result
            assert result["repo_summary_data"] is not None
            assert "REPO SUMMARY" in result["repo_summary_data"]
            assert "Framework" in result["repo_summary_data"]


class TestExecutionNodePatchGuard:
    """Tests for execution_node patch size guard."""

    @pytest.mark.asyncio
    async def test_execution_node_intercepts_requires_split(self, tmp_path):
        """Test that execution_node intercepts requires_split flag."""
        # Create test file and mark it as read
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): pass")

        state: AgentState = {
            "task": "test",
            "history": [],
            "verified_reads": [str(test_file)],
            "next_action": {
                "name": "edit_file",
                "arguments": {"path": "test.py", "patch": "large_patch"},
            },
            "last_result": None,
            "rounds": 0,
            "working_dir": str(tmp_path),
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
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": None,
        }

        config = MagicMock()

        mock_orchestrator = MagicMock()
        mock_orchestrator.cancel_event = None
        mock_orchestrator.execute_tool.return_value = {
            "ok": False,
            "error": "Patch too large (250 lines). Max allowed: 200.",
            "requires_split": True,
        }
        mock_orchestrator._session_read_files = set()
        mock_orchestrator._check_loop_prevention = MagicMock(return_value=False)
        mock_orchestrator.preflight_check = MagicMock(return_value={"ok": True})

        with patch(
            "src.core.orchestration.graph.nodes.execution_node._resolve_orchestrator",
            return_value=mock_orchestrator,
        ):
            result = await execution_node(state, config)

            assert "replan_required" in result
            assert result["replan_required"] is not None
            assert "200" in result["replan_required"]
            assert result["action_failed"] is True


class TestEvaluationNode:
    """Tests for evaluation_node."""

    @pytest.mark.asyncio
    async def test_evaluation_node_complete(self):
        """Test evaluation returns complete when all checks pass."""
        state: AgentState = {
            "task": "test",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 5,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": [{"description": "step 1", "completed": True}],
            "current_step": 1,
            "deterministic": None,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": None,
            "key_symbols": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": True,
            "verification_result": {
                "tests": {"status": "ok"},
                "linter": {"status": "ok"},
                "syntax": {"status": "ok"},
            },
            "step_controller_enabled": False,
            "task_decomposed": None,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": None,
        }

        config = MagicMock()
        result = await evaluation_node(state, config)

        assert result["evaluation_result"] == "complete"

    @pytest.mark.asyncio
    async def test_evaluation_node_replan_on_failure(self):
        """Test evaluation triggers replan when verification fails."""
        state: AgentState = {
            "task": "test",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 5,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": [{"description": "step 1"}],
            "current_step": 0,
            "deterministic": None,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": None,
            "key_symbols": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": False,
            "verification_result": {
                "tests": {"status": "fail", "message": "Test failed"},
                "linter": {"status": "ok"},
                "syntax": {"status": "ok"},
            },
            "step_controller_enabled": False,
            "task_decomposed": None,
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": None,
        }

        config = MagicMock()
        result = await evaluation_node(state, config)

        # Verification failure now routes to "debug" (bounded by debug_attempts) not "replan"
        # debug_attempts is NOT incremented by evaluation_node (NEW-4 fix); debug_node owns the counter.
        assert result["evaluation_result"] == "debug"
        assert "debug_attempts" not in result


class TestReplanNode:
    """Tests for replan_node."""

    @pytest.mark.asyncio
    async def test_replan_node_requires_orchestrator(self):
        """Test replan_node handles missing orchestrator gracefully."""
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
            "current_plan": [{"description": "step 1"}],
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
            "tool_call_count": 0,
            "max_tool_calls": 50,
            "repo_summary_data": None,
            "replan_required": "Patch exceeded 200 lines",
            "action_failed": True,
        }

        config = MagicMock()

        with patch(
            "src.core.orchestration.graph.nodes.replan_node._resolve_orchestrator"
        ) as mock_resolve:
            mock_resolve.return_value = None

            result = await replan_node(state, config)

            assert "errors" in result
            assert result["replan_required"] is None
            assert result["action_failed"] is False
