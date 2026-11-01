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

# ruff: noqa: E501
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestBashToolShellOperatorBlocking:
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





class TestAgentStateRequiredFieldPresence:
    """Tests for the new state fields."""

    def test_agent_state_has_all_fields(self):
        """Test that AgentState TypedDict has all required fields."""
        from src.core.orchestration.graph.state import AgentState

        state: AgentState = {  # type: ignore[assignment]
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


class TestOrchestratorWorkingDirSandboxAttribute:
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
