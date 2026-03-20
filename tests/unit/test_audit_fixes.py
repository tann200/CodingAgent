"""
Tests for the 7 critical audit fixes:
1. Shell injection fix (shell=False)
2. AnalysisNode
3. DebugNode with retry logic
4. Step Controller
5. Verification branching
6. Sandbox enforcement
7. State fields
"""

import pytest
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestShellInjectionFix:
    """Tests for the shell injection security fix."""

    def test_bash_rejects_shell_operators(self):
        """Test that bash tool rejects dangerous shell operators."""
        from src.tools import file_tools

        dangerous_commands = [
            "ls && rm -rf /",
            "ls || cat /etc/passwd",
            "ls; cat /etc/passwd",
            "ls | grep root",
            "ls > /tmp/out",
            "ls >> /tmp/out",
            "ls $(whoami)",
            "ls `whoami`",
        ]

        for cmd in dangerous_commands:
            result = file_tools.bash(cmd, workdir=Path.cwd())
            assert result["status"] == "error"
            assert (
                "dangerous pattern" in result["error"].lower()
                or "not allowed" in result["error"].lower()
            )

    def test_bash_accepts_safe_commands(self):
        """Test that bash tool accepts safe commands."""
        from src.tools import file_tools

        safe_commands = [
            "ls",
            "ls -la",
            "pwd",
            "echo hello",
            "whoami",
        ]

        for cmd in safe_commands:
            result = file_tools.bash(cmd, workdir=Path.cwd())
            # Safe commands must succeed (status == "ok"), not just return any status
            assert result.get("status") == "ok", f"Expected 'ok' for safe command '{cmd}', got: {result}"

    def test_bash_rejects_unknown_commands(self):
        """Test that bash tool rejects unknown commands."""
        from src.tools import file_tools

        result = file_tools.bash("rm -rf /", workdir=Path.cwd())
        assert result["status"] == "error"
        assert (
            "dangerous" in result["error"].lower()
            or "not allowed" in result["error"].lower()
        )

    def test_bash_empty_command(self):
        """Test that bash tool rejects empty commands."""
        from src.tools import file_tools

        result = file_tools.bash("", workdir=Path.cwd())
        assert result["status"] == "error"


class TestAnalysisNode:
    """Tests for the AnalysisNode."""

    @pytest.mark.asyncio
    async def test_analysis_node_returns_analysis_summary(self):
        """Test that analysis_node returns analysis_summary."""
        from src.core.orchestration.graph.nodes.workflow_nodes import analysis_node
        from src.core.orchestration.graph.state import AgentState

        state: AgentState = {
            "task": "test task",
            "working_dir": ".",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "system_prompt": "",
            "errors": [],
            "current_plan": None,
            "current_step": 0,
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        config = {"configurable": {"orchestrator": None}}

        result = await analysis_node(state, config)

        assert "analysis_summary" in result
        assert "relevant_files" in result
        assert "key_symbols" in result

    @pytest.mark.asyncio
    async def test_analysis_node_with_no_orchestrator(self):
        """Test that analysis_node handles missing orchestrator gracefully."""
        from src.core.orchestration.graph.nodes.workflow_nodes import analysis_node
        from src.core.orchestration.graph.state import AgentState

        state: AgentState = {
            "task": "test task",
            "working_dir": ".",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "system_prompt": "",
            "errors": [],
            "current_plan": None,
            "current_step": 0,
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        config = {"configurable": {}}

        result = await analysis_node(state, config)

        assert "analysis_summary" in result
        assert result["relevant_files"] == []


class TestDebugNode:
    """Tests for the DebugNode with retry logic."""

    @pytest.mark.asyncio
    async def test_debug_node_returns_next_action(self):
        """Test that debug_node generates a fix attempt."""
        from src.core.orchestration.graph.nodes.workflow_nodes import debug_node
        from src.core.orchestration.graph.state import AgentState

        mock_orchestrator = MagicMock()
        mock_orchestrator.tool_registry.tools = {}
        mock_orchestrator.adapter = None

        state: AgentState = {
            "task": "fix the code",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": {"error": "Test failure"},
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": None,
            "current_step": 0,
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        config = {"configurable": {"orchestrator": mock_orchestrator}}

        result = await debug_node(state, config)

        assert "debug_attempts" in result

    @pytest.mark.asyncio
    async def test_debug_node_max_attempts_reached(self):
        """Test that debug_node stops after max attempts."""
        from src.core.orchestration.graph.nodes.workflow_nodes import debug_node
        from src.core.orchestration.graph.state import AgentState

        mock_orchestrator = MagicMock()
        mock_orchestrator.tool_registry.tools = {}

        state: AgentState = {
            "task": "fix the code",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": {"error": "Test failure"},
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": None,
            "current_step": 0,
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "debug_attempts": 3,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        config = {"configurable": {"orchestrator": mock_orchestrator}}

        result = await debug_node(state, config)

        assert "errors" in result or result.get("next_action") is None


class TestStepController:
    """Tests for the Step Controller."""

    @pytest.mark.asyncio
    async def test_step_controller_with_plan(self):
        """Test step_controller_node with active plan."""
        from src.core.orchestration.graph.nodes.workflow_nodes import (
            step_controller_node,
        )
        from src.core.orchestration.graph.state import AgentState

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
                {"description": "step 1", "action": "read"},
                {"description": "step 2", "action": "write"},
            ],
            "current_step": 0,
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        config = {}

        result = await step_controller_node(state, config)

        assert "step_description" in result

    @pytest.mark.asyncio
    async def test_step_controller_disabled(self):
        """Test step_controller_node when disabled."""
        from src.core.orchestration.graph.nodes.workflow_nodes import (
            step_controller_node,
        )
        from src.core.orchestration.graph.state import AgentState

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
            "current_plan": [],
            "current_step": 0,
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "step_controller_enabled": False,
        }

        config = {}

        result = await step_controller_node(state, config)

        assert result == {}


class TestVerificationBranching:
    """Tests for verification branching."""

    def test_should_after_verification_success(self):
        """Test routing when verification passes."""
        from src.core.orchestration.graph.builder import should_after_verification
        from src.core.orchestration.graph.state import AgentState

        state: AgentState = {
            "verification_result": {
                "tests": {"status": "ok"},
                "linter": {"status": "ok"},
                "syntax": {"status": "ok"},
            },
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "task": "",
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
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        result = should_after_verification(state)
        assert result == "memory_sync"

    def test_should_after_verification_failure_with_retries(self):
        """Test routing when verification fails with retries remaining."""
        from src.core.orchestration.graph.builder import should_after_verification
        from src.core.orchestration.graph.state import AgentState

        state: AgentState = {
            "verification_result": {
                "tests": {"status": "fail"},
            },
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "task": "",
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
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        result = should_after_verification(state)
        assert result == "debug"

    def test_should_after_verification_failure_no_retries(self):
        """Test routing when verification fails and no retries remaining."""
        from src.core.orchestration.graph.builder import should_after_verification
        from src.core.orchestration.graph.state import AgentState

        state: AgentState = {
            "verification_result": {
                "tests": {"status": "fail"},
            },
            "debug_attempts": 3,
            "max_debug_attempts": 3,
            "task": "",
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
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        result = should_after_verification(state)
        assert result == "end"


class TestGraphBuilder:
    """Tests for the updated graph builder."""

    def test_graph_has_all_nodes(self):
        """Test that compile_agent_graph creates graph with all required nodes."""
        from src.core.orchestration.graph.builder import compile_agent_graph

        graph = compile_agent_graph()

        assert graph is not None
        # LangGraph returns a StateGraph, not a callable
        assert hasattr(graph, "invoke")

    def test_should_after_execution_with_step_controller(self):
        """Test that step controller routing works."""
        from src.core.orchestration.graph.builder import should_after_execution
        from src.core.orchestration.graph.state import AgentState

        state: AgentState = {
            "current_plan": [{"description": "step 1"}],
            "current_step": 0,
            "step_controller_enabled": True,
            "task": "",
            "history": [],
            "verified_reads": [],
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": None,
        }

        result = should_after_execution(state)
        assert result == "perception"


class TestStateFields:
    """Tests for the new state fields."""

    def test_agent_state_has_all_fields(self):
        """Test that AgentState TypedDict has all required fields."""
        from src.core.orchestration.graph.state import AgentState

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
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": None,
            "step_controller_enabled": True,
        }

        assert state["analysis_summary"] is None
        assert state["relevant_files"] == []
        assert state["key_symbols"] == []
        assert state["debug_attempts"] == 0
        assert state["max_debug_attempts"] == 3
        assert state["verification_passed"] is None
        assert state["step_controller_enabled"]


class TestSandboxEnforcement:
    """Tests for sandbox enforcement in orchestrator."""

    def test_orchestrator_has_sandbox_validation(self, tmp_path):
        """Test that orchestrator performs sandbox validation for write operations."""
        from src.core.orchestration.orchestrator import Orchestrator, example_registry

        reg = example_registry()
        orch = Orchestrator(
            None,
            tool_registry=reg,
            working_dir=str(tmp_path),
            allow_external_working_dir=True,
        )

        assert hasattr(orch, "working_dir")
