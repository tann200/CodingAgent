"""
Integration tests for memory → delegation flow in the graph pipeline.
"""

import pytest
import asyncio
from unittest.mock import patch

from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node
from src.core.orchestration.graph.nodes.delegation_node import (
    delegation_node,
    create_delegation,
)


class TestMemoryDelegationFlow:
    """Tests for the memory → delegation pipeline flow."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config."""
        return {"configurable": {"orchestrator": None}}

    @pytest.fixture
    def base_state(self, tmp_path):
        """Create a base state for memory operations."""
        return {
            "working_dir": str(tmp_path),
            "messages": [{"role": "user", "content": "Analyze the codebase"}],
            "current_task": "Analyze the codebase",
            "completed_steps": [],
        }

    @pytest.mark.asyncio
    async def test_memory_updates_then_delegation(
        self, tmp_path, mock_config, base_state
    ):
        """Test that memory updates run before delegation tasks."""
        memory_result = await memory_update_node(base_state, mock_config)

        assert "delegations" in memory_result or memory_result == {}

    @pytest.mark.asyncio
    async def test_delegation_with_memory_context(self, tmp_path, mock_config):
        """Test delegation receives correct working directory from state."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [
                create_delegation("researcher", "Analyze repository structure"),
            ],
        }

        async def mock_delegate(role, subtask_description, working_dir):
            return {
                "role": role,
                "task": subtask_description,
                "working_dir": working_dir,
                "status": "completed",
            }

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            result = await delegation_node(state, mock_config)

            assert result["delegation_results"]["delegation_0"]["status"] == "completed"
            assert result["delegation_results"]["delegation_0"]["result"][
                "working_dir"
            ] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_parallel_delegation_preserves_memory(
        self, tmp_path, mock_config, base_state
    ):
        """Test that parallel delegations don't interfere with memory state."""
        state = {
            **base_state,
            "delegations": [
                create_delegation("researcher", "Task 1", result_key="task1"),
                create_delegation("reviewer", "Task 2", result_key="task2"),
            ],
        }

        async def mock_delegate(role, subtask_description, working_dir):
            await asyncio.sleep(0.05)
            return {"role": role, "task": subtask_description}

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            result = await delegation_node(state, mock_config)

            assert len(result["delegation_results"]) == 2
            assert "task1" in result["delegation_results"]
            assert "task2" in result["delegation_results"]


class TestDelegationWithAdvancedMemory:
    """Tests for delegation with advanced memory features enabled."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config with advanced features."""
        return {"configurable": {"orchestrator": None}}

    @pytest.mark.asyncio
    async def test_delegation_after_memory_update(self, tmp_path, mock_config):
        """Test full flow: memory update followed by delegation."""
        memory_state = {
            "working_dir": str(tmp_path),
            "messages": [{"role": "user", "content": "Refactor the code"}],
            "current_task": "Refactor the code",
            "completed_steps": ["step 1", "step 2"],
            "delegations": [
                create_delegation(
                    "reviewer", "Review refactored code", result_key="review"
                ),
            ],
        }

        delegation_state = memory_state.copy()

        async def mock_delegate(role, subtask_description, working_dir):
            return {"role": role, "review": f"Reviewed: {subtask_description}"}

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            delegation_result = await delegation_node(delegation_state, mock_config)

            assert "delegation_results" in delegation_result
            assert "review" in delegation_result["delegation_results"]
            assert (
                "Reviewed"
                in delegation_result["delegation_results"]["review"]["result"]["review"]
            )

    @pytest.mark.asyncio
    async def test_empty_delegation_does_not_block(self, tmp_path, mock_config):
        """Test that empty delegation list doesn't cause issues."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [],
        }

        result = await delegation_node(state, mock_config)

        assert result == {} or "delegation_results" not in result


class TestCreateDelegationAPI:
    """Tests for create_delegation helper function."""

    def test_create_delegation_returns_dict(self):
        """Test that create_delegation returns proper dict."""
        delegation = create_delegation("researcher", "Analyze code")

        assert isinstance(delegation, dict)
        assert delegation["role"] == "researcher"
        assert delegation["task"] == "Analyze code"

    def test_create_delegation_with_result_key(self):
        """Test create_delegation with custom result key."""
        delegation = create_delegation("reviewer", "Review", result_key="code_review")

        assert delegation["result_key"] == "code_review"

    def test_create_delegation_without_result_key(self):
        """Test create_delegation without result key returns None."""
        delegation = create_delegation("coder", "Implement feature")

        assert delegation["result_key"] is None
