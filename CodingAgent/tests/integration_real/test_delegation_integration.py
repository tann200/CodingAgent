"""
Real integration tests for delegation (subagent spawning and execution).

Tests verify that delegation infrastructure works correctly with minimal mocking.
"""

import pytest
from pathlib import Path


@pytest.mark.integration_real
@pytest.mark.skip(reason="Requires delegation infrastructure refactoring - implement after tool chain tests pass")
class TestDelegationIntegration:
    """Tests for real delegation integration (not mocked)."""

    def test_subagent_spawns_and_executes(self, tmp_path: Path):
        """
        Integration test: Verify subagent actually spawns and executes.
        
        This test verifies:
        1. Parent agent calls delegate_task_async
        2. Subagent actually spawns (not mocked)
        3. Subagent executes task
        4. Results are returned to parent
        5. Parent agent state is updated with subagent results
        
        NOTE: This test should NOT mock delegate_task_async.
        """
        from src.core.orchestration.orchestrator import Orchestrator
        from src.core.inference.adapters.mock_adapter import MockAdapter
        
        # Setup parent orchestrator
        parent_orch = Orchestrator(working_dir=str(tmp_path))
        
        # Create test file for subagent to analyze
        test_file = tmp_path / "buggy_code.py"
        test_file.write_text("""
def calculate(x, y):
    return x / y  # BUG: No zero check

def process(items):
    for item in items:
        print(item)  # BUG: Should return processed items
""")
        
        # Parent agent responses that trigger delegation
        parent_responses = [
            """I'll delegate code analysis to a specialist.
<tool_calls>
[{"name": "delegate_task", "arguments": {
    "role": "analyst",
    "subtask_description": "Analyze buggy_code.py for potential bugs and return findings",
    "working_dir": "%s"
}}]
</tool_calls>""" % str(tmp_path),
            
            "Analysis complete. The subagent found 2 bugs in the code."
        ]
        
        parent_adapter = MockAdapter(responses=parent_responses)
        parent_orch._llm_manager._default_adapter = parent_adapter
        
        # Execute parent agent
        result = parent_orch.run_agent_once(
            messages=[{
                "role": "user",
                "content": "Analyze buggy_code.py for potential issues"
            }]
        )
        
        # Verify delegation occurred
        # TODO: Add assertions based on delegation infrastructure API
        # - Check that delegate_task_async was actually called
        # - Verify subagent spawned and executed
        # - Verify results were returned
        assert result.get("ok") or result.get("assistant_message")

    def test_delegation_result_merging(self, tmp_path: Path):
        """
        Integration test: Verify subagent results merge into parent state.
        
        This test verifies:
        1. Subagent executes and returns structured results
        2. Results are properly formatted
        3. Parent agent receives and can use results
        4. Session state reflects delegation occurred
        """
        # TODO: Implement after delegation infrastructure is available
        pytest.skip("Delegation infrastructure requires refactoring")

    def test_nested_delegation(self, tmp_path: Path):
        """
        Integration test: Verify nested delegation (subagent spawns sub-subagent).
        
        This test verifies:
        1. Subagent can delegate to another subagent
        2. Results propagate up the chain correctly
        3. No infinite delegation loops
        4. Depth limits are enforced
        """
        # TODO: Implement after delegation infrastructure is available
        pytest.skip("Delegation infrastructure requires refactoring")


# NOTE: These tests are currently skipped because the delegation infrastructure
# needs refactoring to support real (non-mocked) delegation testing.
#
# Current delegation tests that mock delegate_task_async have been moved to:
# tests/unit/orchestration/test_delegation_node_unit.py
#
# Once delegation infrastructure is refactored, implement these tests to verify:
# - Real subagent spawning
# - Task isolation
# - Result merging
# - Error propagation
# - Depth limits
