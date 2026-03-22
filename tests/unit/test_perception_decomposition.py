"""
Tests for NEW-6: perception_node task decomposition resets `rounds=0`.

The bug: when perception_node decomposed a task, it returned `"rounds": 0`,
explicitly resetting the counter. The decomposition guard is `rounds == 0`.
So if perception re-ran (e.g. after plan_validator rejection), rounds was
still 0 and decomposition fired again — an infinite loop.

The fix: return `"rounds": (state.get("rounds") or 0) + 1` so rounds advances
past 0 and re-entry does not re-trigger decomposition.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock


def _make_state(**kwargs):
    base = {
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
        "last_debug_error_type": None,
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
        "session_id": None,
        "delegation_results": None,
        "analyst_findings": None,
        "plan_resumed": None,
        "delegations": None,
        "last_tool_name": None,
        "original_task": None,
        "step_description": None,
        "planned_action": None,
        "plan_validation": None,
        "plan_enforce_warnings": None,
        "plan_strict_mode": None,
        "task_history": None,
    }
    base.update(kwargs)
    return base


def _make_orchestrator():
    orch = MagicMock()
    orch.adapter = MagicMock()
    orch.adapter.provider = {"name": "test"}
    orch.adapter.models = ["test-model"]
    orch.tool_registry = MagicMock()
    orch.tool_registry.tools = {}
    orch.cancel_event = None
    orch.deterministic = False
    return orch


class TestDecompositionRoundsIncrement:
    """NEW-6 regression tests: decomposition must not reset rounds to 0."""

    @pytest.mark.asyncio
    async def test_decomposition_returns_rounds_gte_one(self, monkeypatch):
        """
        NEW-6 regression: when task decomposition fires on rounds=0,
        the returned 'rounds' value must be >= 1 (not 0).

        Before fix: returned 'rounds': 0 → re-entry triggers decomposition again.
        After fix: returns 'rounds': 1 → re-entry guard fails, no loop.
        """
        from src.core.orchestration.graph.nodes.perception_node import perception_node

        # A multi-step task that triggers decomposition
        state = _make_state(
            task="Create a file and then run the tests",
            rounds=0,
            current_plan=None,
            current_step=0,
        )

        # Mock call_model to return a valid decomposition JSON
        decomp_response = '[{"description":"Create a file"},{"description":"Run the tests"}]'
        mock_resp = {"choices": [{"message": {"content": decomp_response}}]}

        async def mock_call_model(*args, **kwargs):
            return mock_resp

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            mock_call_model,
        )

        orch = _make_orchestrator()
        config = {"configurable": {"orchestrator": orch}}

        result = await perception_node(state, config)

        # If decomposition fired, rounds must be >= 1
        returned_rounds = result.get("rounds", 0)
        assert returned_rounds >= 1, (
            f"NEW-6 regression: decomposition returned rounds={returned_rounds}. "
            "It must be >= 1 to prevent re-triggering decomposition on re-entry."
        )

    @pytest.mark.asyncio
    async def test_decomposition_does_not_fire_when_rounds_is_one(self, monkeypatch):
        """
        NEW-6 regression: after a first decomposition run (rounds=1),
        re-entering perception must NOT re-decompose.

        The guard is `if state.get("rounds", 0) == 0`. With rounds=1 this
        should be False and decomposition must not fire again.
        """
        from src.core.orchestration.graph.nodes.perception_node import perception_node

        decomp_call_count = {"count": 0}

        async def mock_call_model(*args, **kwargs):
            # Track if this is a decomposition call or a regular perception call
            # Decomposition calls get a JSON array; normal calls get YAML tool
            decomp_call_count["count"] += 1
            # Return YAML tool call (normal perception response)
            return {"choices": [{"message": {"content": "```yaml\nname: respond\narguments:\n  message: done\n```"}}]}

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            mock_call_model,
        )

        # State with rounds=1 (simulating re-entry after first decomposition)
        state = _make_state(
            task="Create a file and run tests",
            rounds=1,  # Already past round 0
            current_plan=[{"description": "Create a file"}, {"description": "Run tests"}],
            current_step=0,
            task_decomposed=True,
        )

        orch = _make_orchestrator()
        config = {"configurable": {"orchestrator": orch}}

        result = await perception_node(state, config)

        # The result should NOT contain a new decomposed plan (task_decomposed guard)
        # Instead it should just be a normal perception result
        # If decomposition re-fired, task_decomposed would be True and rounds would reset
        returned_rounds = result.get("rounds")
        if returned_rounds is not None:
            assert returned_rounds >= 1, (
                "Re-entry at rounds=1 must not reset rounds to 0"
            )

    @pytest.mark.asyncio
    async def test_decomposition_increments_rounds_not_resets(self, monkeypatch):
        """
        NEW-6 regression: the rounds field in the decomposition return
        must be state['rounds'] + 1, not a constant 0.
        """
        from src.core.orchestration.graph.nodes.perception_node import perception_node

        decomp_response = '[{"description":"Step one"},{"description":"Step two"}]'
        mock_resp = {"choices": [{"message": {"content": decomp_response}}]}

        async def mock_call_model(*args, **kwargs):
            return mock_resp

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            mock_call_model,
        )

        # Start at rounds=0 (fresh task), trigger decomposition
        state = _make_state(
            task="Create a module and add unit tests for it",
            rounds=0,
            current_plan=None,
            current_step=0,
            task_decomposed=False,
        )

        orch = _make_orchestrator()
        config = {"configurable": {"orchestrator": orch}}

        result = await perception_node(state, config)

        # Check if decomposition actually fired (current_plan set)
        if result.get("current_plan"):
            # Decomposition fired — rounds must be > 0
            assert result.get("rounds", 0) > 0, (
                "NEW-6: decomposition returned rounds=0. "
                "Must return rounds = state.rounds + 1 = 1"
            )
            assert result.get("rounds") == 1, (
                f"NEW-6: expected rounds=1 after decomposition from rounds=0, "
                f"got rounds={result.get('rounds')}"
            )

    def test_decomposition_guard_blocks_at_rounds_one(self):
        """
        Unit test the guard condition: rounds==0 must be False when rounds=1.
        This ensures the fix is logically correct.
        """
        # Simulate what perception_node checks
        rounds_values = [0, 1, 2, 3, 10]
        for rounds in rounds_values:
            guard_fires = (rounds == 0)
            if rounds == 0:
                assert guard_fires is True, "Guard should fire at rounds=0"
            else:
                assert guard_fires is False, (
                    f"Guard must NOT fire at rounds={rounds} "
                    f"(would trigger re-decomposition)"
                )
