"""
Tests for delegation_node and delegation integration.
"""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock

from src.core.orchestration.graph.nodes.delegation_node import (
    delegation_node,
    create_delegation,
)


class TestCreateDelegation:
    """Tests for create_delegation helper function."""

    def test_create_delegation_basic(self):
        """Test creating a basic delegation."""
        delegation = create_delegation("researcher", "Analyze the codebase")

        assert delegation["role"] == "researcher"
        assert delegation["task"] == "Analyze the codebase"
        assert delegation["result_key"] is None

    def test_create_delegation_with_result_key(self):
        """Test creating a delegation with custom result key."""
        delegation = create_delegation(
            "reviewer", "Review changes", result_key="code_review"
        )

        assert delegation["role"] == "reviewer"
        assert delegation["task"] == "Review changes"
        assert delegation["result_key"] == "code_review"

    def test_create_delegation_all_roles(self):
        """Test creating delegations for all valid roles."""
        for role in ["researcher", "coder", "reviewer", "planner"]:
            delegation = create_delegation(role, f"Test {role}")
            assert delegation["role"] == role


class TestDelegationNodeUnit:
    """Unit tests for delegation_node."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config."""
        return {"configurable": {"orchestrator": None}}

    @pytest.fixture
    def base_state(self, tmp_path):
        """Create a base state."""
        return {
            "working_dir": str(tmp_path),
            "delegations": [],
        }

    @pytest.mark.asyncio
    async def test_no_delegations_returns_empty(self, base_state, mock_config):
        """Test that empty delegations list returns empty dict."""
        result = await delegation_node(base_state, mock_config)
        assert result == {}

    @pytest.mark.asyncio
    async def test_single_delegation(self, tmp_path, mock_config):
        """Test processing a single delegation."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [
                create_delegation("researcher", "Analyze structure"),
            ],
        }

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            new_callable=AsyncMock,
            return_value={"result": "analysis complete"},
        ) as mock_delegate:
            result = await delegation_node(state, mock_config)

            mock_delegate.assert_called_once()
            assert "delegation_results" in result
            assert len(result["delegation_results"]) == 1

    @pytest.mark.asyncio
    async def test_multiple_delegations_parallel(self, tmp_path, mock_config):
        """Test that multiple delegations run in parallel."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [
                create_delegation("researcher", "Task 1", result_key="task1"),
                create_delegation("reviewer", "Task 2", result_key="task2"),
                create_delegation("coder", "Task 3", result_key="task3"),
            ],
        }

        results = []

        async def mock_delegate(role, subtask_description, working_dir):
            results.append(subtask_description)
            await asyncio.sleep(0.1)  # Simulate work
            return {"result": f"completed {subtask_description}"}

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            import time

            start = time.time()
            result = await delegation_node(state, mock_config)
            elapsed = time.time() - start

            # If run sequentially, would take ~0.3s. Parallel should be ~0.1s
            assert elapsed < 0.25, "Delegations should run in parallel"
            assert len(results) == 3
            assert "delegation_results" in result
            assert len(result["delegation_results"]) == 3

    @pytest.mark.asyncio
    async def test_delegation_error_handling(self, tmp_path, mock_config):
        """Test that delegation errors are captured in results."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [
                create_delegation("researcher", "Failing task", result_key="fail"),
            ],
        }

        async def mock_delegate_error(role, task, working_dir):
            raise ValueError("Task failed")

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            side_effect=mock_delegate_error,
        ):
            result = await delegation_node(state, mock_config)

            assert "delegation_results" in result
            assert "fail" in result["delegation_results"]
            assert "error" in result["delegation_results"]["fail"]

    @pytest.mark.asyncio
    async def test_empty_task_skipped(self, tmp_path, mock_config):
        """Test that delegations with empty tasks are skipped."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [
                create_delegation("researcher", "", result_key="empty"),
                create_delegation("reviewer", "Valid task", result_key="valid"),
            ],
        }

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            new_callable=AsyncMock,
            return_value={"result": "ok"},
        ) as mock_delegate:
            result = await delegation_node(state, mock_config)

            # Only one delegation should have been called
            assert mock_delegate.call_count == 1
            assert len(result["delegation_results"]) == 1
            assert "valid" in result["delegation_results"]


class TestDelegationIntegration:
    """Integration tests for delegation with full orchestration."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config."""
        return {"configurable": {"orchestrator": None}}

    @pytest.mark.asyncio
    async def test_delegation_results_stored_correctly(self, tmp_path, mock_config):
        """Test that delegation results are stored with correct keys."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [
                create_delegation("researcher", "Research task", result_key="research"),
                create_delegation("reviewer", "Review task", result_key="review"),
            ],
        }

        async def mock_delegate(role, subtask_description, working_dir):
            return {"role": role, "task": subtask_description, "status": "completed"}

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            result = await delegation_node(state, mock_config)

            assert result["delegation_results"]["research"]["status"] == "completed"
            assert (
                result["delegation_results"]["research"]["result"]["role"]
                == "researcher"
            )
            assert (
                result["delegation_results"]["review"]["result"]["role"] == "reviewer"
            )

    @pytest.mark.asyncio
    async def test_default_result_key_generated(self, tmp_path, mock_config):
        """Test that default result keys are generated when not specified."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [
                create_delegation("researcher", "Task 1"),
            ],
        }

        async def mock_delegate(role, subtask_description, working_dir):
            return {"result": "ok"}

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            result = await delegation_node(state, mock_config)

            # Should have a default key
            keys = list(result["delegation_results"].keys())
            assert len(keys) == 1
            assert keys[0].startswith("delegation_")


class TestDelegationMemoryWiring:
    """Tests that delegations are correctly wired to memory system."""

    @pytest.mark.asyncio
    async def test_delegation_includes_working_dir(self, tmp_path):
        """Test that delegations receive the correct working directory."""
        state = {
            "working_dir": str(tmp_path),
            "delegations": [
                create_delegation("researcher", "Analyze codebase"),
            ],
        }
        config = {"configurable": {"orchestrator": None}}

        captured_args = {}

        async def mock_delegate(role, subtask_description, working_dir):
            captured_args["working_dir"] = working_dir
            return {"result": "ok"}

        with patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            side_effect=mock_delegate,
        ):
            await delegation_node(state, config)

            assert captured_args["working_dir"] == str(tmp_path)


class TestDelegationHelper:
    """Tests for the delegation helper function."""

    def test_create_delegation_dict_structure(self):
        """Test that create_delegation returns proper dict structure."""
        delegation = create_delegation("test_role", "test_task", "test_key")

        assert isinstance(delegation, dict)
        assert set(delegation.keys()) == {"role", "task", "result_key"}

    def test_create_delegation_none_values(self):
        """Test handling of None values in delegation."""
        delegation = create_delegation(None, None, None)

        # Should handle None gracefully
        assert delegation.get("role") is None
        assert delegation.get("task") is None
        assert delegation.get("result_key") is None
