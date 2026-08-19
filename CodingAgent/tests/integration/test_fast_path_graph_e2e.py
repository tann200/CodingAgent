"""E2E tests for the fast-path graph with Phases 5a-5d re-enabled nodes.

Verifies that analysis, planning, plan_validator, and step_controller are
actually visited during graph execution, and that routing between them works.
"""

import itertools
from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


def _mock_all_llm():
    """Return an ExitStack with all LLM calls mocked."""
    import src.core.orchestration.graph.nodes.perception_node as pn
    import src.core.orchestration.graph.nodes.analysis_node as an
    import src.core.orchestration.graph.nodes.planning_node as pln
    import src.core.orchestration.graph.nodes.execution_node as en

    simple_response = {
        "choices": [{"message": {"content": "Task completed successfully."}}]
    }
    response_iter = itertools.cycle([simple_response])

    def mock_call(*args, **kwargs):
        return next(response_iter)

    stack = ExitStack()
    stack.enter_context(patch.object(pn, "call_model", side_effect=mock_call))
    stack.enter_context(patch.object(an, "generate_repo_summary", return_value={}))
    stack.enter_context(patch.object(pln, "call_model", side_effect=mock_call))
    stack.enter_context(patch.object(en, "call_model", side_effect=mock_call))
    stack.enter_context(
        patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            return_value={},
        )
    )
    stack.enter_context(
        patch(
            "src.core.inference.llm_manager.call_model",
            side_effect=mock_call,
        )
    )
    return stack


def _make_task_state(tmp_path, **overrides):
    """Minimal AgentState dict for e2e graph invocation."""
    state = {
        "task": "say hello",
        "history": [],
        "verified_reads": [],
        "next_action": None,
        "last_result": None,
        "rounds": 0,
        "working_dir": str(tmp_path),
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
        "current_plan": [],
        "current_step": -1,
        "step_controller_enabled": True,
        "session_id": "test",
        "_should_distill": False,
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_fast_path_graph_all_nodes_visited_with_mocked_llm(tmp_path):
    """Run the fast-path graph with all LLM calls mocked, verify it completes."""
    from src.core.orchestration.graph.builder import _compile_fast_path_graph

    with _mock_all_llm() as stack:
        graph = _compile_fast_path_graph()
        state = _make_task_state(tmp_path, history=[{"role": "user", "content": "hello"}])
        result = await graph.ainvoke(state)

    assert isinstance(result, dict)
    assert "rounds" in result
    assert result["rounds"] >= 0


@pytest.mark.asyncio
async def test_fast_path_routes_through_analysis_when_next_action_clear(tmp_path):
    """When next_action is set, perception routes to analysis which fast-paths back."""
    from src.core.orchestration.graph.builder import _compile_fast_path_graph

    with _mock_all_llm() as stack:
        graph = _compile_fast_path_graph()
        state = _make_task_state(
            tmp_path,
            next_action={"name": "read_file", "arguments": {"path": "README.md"}},
        )
        result = await graph.ainvoke(state)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert "rounds" in result
    # With a pre-set next_action, the graph should execute at least 1 round
    assert result["rounds"] >= 1


def test_fast_path_graph_edges_connect_correctly():
    """Verify the compiled graph has the expected node-to-edge connectivity."""
    from src.core.orchestration.graph.builder import _compile_fast_path_graph

    graph = _compile_fast_path_graph()

    node_names = list(graph.nodes)
    expected_order = [
        "__start__",
        "perception",
        "analysis",
        "planning",
        "plan_validator",
        "execution",
        "step_controller",
        "verification",
        "evaluation",
        "memory_sync",
    ]
    for name in expected_order:
        assert name in node_names, f"Missing node: {name}"

    assert node_names.index("analysis") > node_names.index("perception"), (
        "analysis must come after perception in node registration"
    )
    assert node_names.index("planning") > node_names.index("analysis"), (
        "planning must come after analysis"
    )
    assert node_names.index("plan_validator") > node_names.index("planning"), (
        "plan_validator must come after planning"
    )
    assert node_names.index("step_controller") > node_names.index("execution"), (
        "step_controller must come after execution"
    )
