"""
Tests for NEW-1 (await fix) and NEW-4 (double-increment fix) in debug_node.

NEW-1: debug_node was missing `await` on call_model, meaning `resp` was a coroutine
       object instead of a dict, so content was always empty and no fix was ever generated.

NEW-4: evaluation_node was incrementing debug_attempts before routing to debug, and then
       debug_node incremented again — effectively consuming 2 budget units per cycle.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _make_base_state(**kwargs):
    """Return a minimal AgentState-compatible dict for debug node tests."""
    base = {
        "task": "fix the bug",
        "history": [],
        "verified_reads": [],
        "next_action": None,
        "last_result": {"error": "NameError: name 'x' is not defined"},
        "rounds": 1,
        "working_dir": ".",
        "system_prompt": "",
        "errors": [],
        "current_plan": None,
        "current_step": 0,
        "deterministic": None,
        "seed": None,
        "analysis_summary": None,
        "relevant_files": None,
        "key_symbols": None,
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "last_debug_error_type": None,
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": False,
        "task_decomposed": None,
        "tool_last_used": None,
        "tool_call_count": 0,
        "max_tool_calls": 50,
        "files_read": None,
        "repo_summary_data": None,
        "replan_required": None,
        "action_failed": None,
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


def _make_mock_orchestrator(tools=None):
    """Create a mock orchestrator with a tool_registry."""
    orch = MagicMock()
    orch.adapter = MagicMock()
    orch.adapter.provider = {"name": "test_provider"}
    orch.adapter.models = ["test_model"]
    tool_registry = MagicMock()
    tool_registry.tools = tools or {
        "edit_file": {"description": "Edit a file"},
        "write_file": {"description": "Write a file"},
    }
    orch.tool_registry = tool_registry
    return orch


# ---------------------------------------------------------------------------
# NEW-1: call_model must be awaited
# ---------------------------------------------------------------------------

class TestDebugNodeAwaitsCallModel:
    """Regression tests for NEW-1 — verify call_model is properly awaited."""

    @pytest.mark.asyncio
    async def test_call_model_is_awaited_returns_dict(self, monkeypatch):
        """
        NEW-1 regression: debug_node must await call_model so `resp` is a dict.

        Before the fix: `resp = call_model(...)` returned a coroutine object.
        `isinstance(resp, dict)` was always False, content was always "", and
        parse_tool_block always returned None.

        After the fix: `resp = await call_model(...)` returns a proper dict,
        which lets content extraction work and parse_tool_block find a tool.
        """
        from src.core.orchestration.graph.nodes.debug_node import debug_node

        # Simulate call_model returning a valid YAML tool call response
        yaml_response = "```yaml\nname: edit_file\narguments:\n  path: fix.py\n  content: fixed\n```"
        mock_resp = {"choices": [{"message": {"content": yaml_response}}]}

        # Mock call_model as a proper async function (not a sync one)
        async def mock_call_model(*args, **kwargs):
            return mock_resp

        # Mock ContextBuilder so we don't need real files
        mock_builder = MagicMock()
        mock_builder.build_prompt.return_value = [{"role": "user", "content": "debug"}]

        # Mock parse_tool_block to return a tool call when content is non-empty
        expected_tool = {"name": "edit_file", "arguments": {"path": "fix.py", "content": "fixed"}}

        def mock_parse_tool_block(content):
            if content and len(content) > 0:
                return expected_tool
            return None

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.call_model",
            mock_call_model,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.ContextBuilder",
            lambda *_a, **_kw: mock_builder,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.parse_tool_block",
            mock_parse_tool_block,
        )

        state = _make_base_state(debug_attempts=0, max_debug_attempts=3)
        config = {"configurable": {"orchestrator": _make_mock_orchestrator()}}

        result = await debug_node(state, config)

        # KEY ASSERTION: next_action must be non-None; only possible if call_model
        # was awaited and returned a dict that contained valid YAML
        assert result.get("next_action") is not None, (
            "NEW-1 regression: next_action is None — call_model may not be awaited"
        )
        assert result["next_action"] == expected_tool

    @pytest.mark.asyncio
    async def test_debug_node_increments_attempts_on_successful_llm_response(self, monkeypatch):
        """debug_node must return debug_attempts=1 after one successful call."""
        from src.core.orchestration.graph.nodes.debug_node import debug_node

        yaml_response = "```yaml\nname: write_file\narguments:\n  path: a.py\n  content: ok\n```"
        mock_resp = {"choices": [{"message": {"content": yaml_response}}]}

        async def mock_call_model(*args, **kwargs):
            return mock_resp

        mock_builder = MagicMock()
        mock_builder.build_prompt.return_value = [{"role": "user", "content": "debug"}]

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.call_model",
            mock_call_model,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.ContextBuilder",
            lambda *_a, **_kw: mock_builder,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.parse_tool_block",
            lambda c: {"name": "write_file", "arguments": {"path": "a.py"}} if c else None,
        )

        state = _make_base_state(debug_attempts=0, max_debug_attempts=3)
        config = {"configurable": {"orchestrator": _make_mock_orchestrator()}}

        result = await debug_node(state, config)

        # debug_attempts should advance from 0 to 1
        assert result.get("debug_attempts") == 1

    @pytest.mark.asyncio
    async def test_debug_node_returns_error_when_parse_fails(self, monkeypatch):
        """debug_node must still return a dict even if parse_tool_block returns None."""
        from src.core.orchestration.graph.nodes.debug_node import debug_node

        mock_resp = {"choices": [{"message": {"content": "no yaml here"}}]}

        async def mock_call_model(*args, **kwargs):
            return mock_resp

        mock_builder = MagicMock()
        mock_builder.build_prompt.return_value = [{"role": "user", "content": "debug"}]

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.call_model",
            mock_call_model,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.ContextBuilder",
            lambda *_a, **_kw: mock_builder,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.parse_tool_block",
            lambda c: None,
        )

        state = _make_base_state(debug_attempts=0, max_debug_attempts=3)
        config = {"configurable": {"orchestrator": _make_mock_orchestrator()}}

        result = await debug_node(state, config)

        # Even with no tool, must return a dict with attempts incremented
        assert isinstance(result, dict)
        assert result.get("next_action") is None
        assert result.get("debug_attempts") == 1
        assert len(result.get("errors", [])) > 0


# ---------------------------------------------------------------------------
# NEW-4: evaluation_node must NOT increment debug_attempts
# ---------------------------------------------------------------------------

class TestEvaluationNodeDoesNotIncrementDebugAttempts:
    """
    Regression tests for NEW-4 — evaluation_node must NOT include debug_attempts
    in its return dict when routing to debug.

    Before the fix: evaluation_node returned {"debug_attempts": debug_attempts + 1}
    as part of the debug-routing branch. Combined with debug_node's own increment,
    each cycle consumed 2 budget units instead of 1.
    """

    @pytest.mark.asyncio
    async def test_evaluation_does_not_set_debug_attempts_key(self):
        """
        NEW-4 regression: evaluation_node routing to debug must NOT include
        debug_attempts in its returned dict (debug_node owns the counter).
        """
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        state = {
            "task": "fix tests",
            "history": [],
            "verified_reads": [],
            "rounds": 2,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": [{"description": "step 1", "completed": True}],
            "current_step": 1,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": False,
            "verification_result": {
                "tests": {"status": "fail", "message": "test failed", "stdout": "FAILED"},
            },
            "evaluation_result": None,
            "next_action": None,
            "last_result": None,
        }
        config = {}

        result = await evaluation_node(state, config)

        # Must route to debug since verification failed
        assert result.get("evaluation_result") == "debug", (
            "evaluation_node should route to 'debug' when verification fails"
        )

        # KEY ASSERTION: evaluation_node must NOT set debug_attempts
        # Only debug_node should own this counter
        assert "debug_attempts" not in result, (
            "NEW-4 regression: evaluation_node must not set debug_attempts; "
            "only debug_node should own that counter"
        )

    @pytest.mark.asyncio
    async def test_full_cycle_attempts_increments_by_one(self, monkeypatch):
        """
        NEW-4 integration: after a full evaluation→debug cycle, debug_attempts should
        increase by exactly 1 (not 2 as with the double-increment bug).
        """
        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node
        from src.core.orchestration.graph.nodes.debug_node import debug_node

        # Step 1: evaluation_node routes to debug
        eval_state = {
            "task": "fix tests",
            "history": [],
            "verified_reads": [],
            "rounds": 2,
            "working_dir": ".",
            "system_prompt": "",
            "errors": [],
            "current_plan": [{"description": "step 1", "completed": True}],
            "current_step": 1,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "verification_passed": False,
            "verification_result": {
                "tests": {"status": "fail", "message": "fail", "stdout": "FAILED test"},
            },
            "evaluation_result": None,
            "next_action": None,
            "last_result": None,
        }
        eval_result = await evaluation_node(eval_state, {})
        assert eval_result.get("evaluation_result") == "debug"

        # Merge what evaluation returned back into state (simulate LangGraph reducer)
        merged_state = dict(eval_state)
        merged_state.update(eval_result)
        # debug_attempts stays at 0 if evaluation_node doesn't touch it
        starting_attempts = merged_state.get("debug_attempts", 0)

        # Step 2: debug_node runs (mock the LLM call)
        async def mock_call_model(*args, **kwargs):
            return {"choices": [{"message": {"content": ""}}]}

        mock_builder = MagicMock()
        mock_builder.build_prompt.return_value = [{"role": "user", "content": "debug"}]

        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.call_model",
            mock_call_model,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.ContextBuilder",
            lambda *_a, **_kw: mock_builder,
        )
        monkeypatch.setattr(
            "src.core.orchestration.graph.nodes.debug_node.parse_tool_block",
            lambda c: None,
        )

        debug_config = {"configurable": {"orchestrator": _make_mock_orchestrator()}}
        debug_result = await debug_node(merged_state, debug_config)

        final_attempts = debug_result.get("debug_attempts", starting_attempts)

        # The increment should be exactly 1 per cycle
        assert final_attempts == starting_attempts + 1, (
            f"NEW-4: debug_attempts should go from {starting_attempts} to "
            f"{starting_attempts + 1}, but got {final_attempts}. "
            "Double-increment would give starting_attempts + 2."
        )
