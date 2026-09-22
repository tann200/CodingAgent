"""
Real integration tests for delegation (subagent spawning and execution).

Per the integration_real README, "real" delegation means delegate_task_async
is NOT mocked: a sub-orchestrator must actually spawn, execute, and merge its
results into the parent session state.

Status: SKIPPED. Real delegation spawns a sub-orchestrator whose LLM calls
must be replayed too; the scripted DeterministicAdapter used by the tool-chain
suite is not yet wired through the sub-agent path, so these tests cannot assert
real subagent behavior deterministically. Un-skip once the sub-orchestrator
shares the parent's scripted adapter and its task/result merging is verified.
"""

import pytest


pytestmark = pytest.mark.integration_real


@pytest.mark.skip(reason="Requires sub-orchestrator to share the scripted LLM adapter")
class TestDelegationIntegration:
    """Tests for real delegation integration (not mocked)."""

    def test_subagent_spawns_and_executes(self, tmp_path):
        """Parent delegates to a real sub-agent that completes an analysis task.

        Implementation sketch (mirror the tool-chain loop wiring):
        - Build the parent Orchestrator with a DeterministicAdapter scripted to
          emit ``delegate_task`` with role/subtask_description/working_dir.
        - Patch the ``_CALL_MODEL_TARGETS`` call_model modules (same set as
          tests/integration_real/test_tool_chain_integration.py) so the child's
          LLM calls are replayed by the same adapter.
        - Authenticate delegation: verify task result is merged into parent
          session state, and subagent files/temp dirs are isolated.
        """
        pytest.skip("Requires sub-orchestrator adapter wiring")

    def test_delegation_result_merging(self, tmp_path):
        """Subagent results merge into the parent agent's session state.

        Requires the sub-agent completion to flow back through the delegation
        node into ``AgentState`` and the parent's message history. Cannot be
        asserted until the sub-orchestrator runs against the scripted adapter.
        """
        pytest.skip("Requires sub-orchestrator adapter wiring")

    def test_nested_delegation(self, tmp_path):
        """Subagent delegates to a sub-subagent without infinite loops.

        Requires per-level task isolation plus depth-limit enforcement. Deferred
        until single-level delegation is un-skipped and stable.
        """
        pytest.skip("Requires sub-orchestrator adapter wiring")


# NOTE: These tests are skipped because real (non-mocked) delegation requires
# the sub-orchestrator to consume the parent's scripted adapter; until then the
# only delegation coverage lives in mocked unit tests:
# tests/unit/orchestration/test_delegation_node_unit.py
#
# When re-enabling, verify:
# - Real subagent spawning (delegate_task_async not mocked)
# - Task isolation (subagent workdir/temp files)
# - Result merging into parent state/messages
# - Error propagation and depth limits
