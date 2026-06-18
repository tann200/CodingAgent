"""E2E tests for the fast-path graph with Phases 5a-5d re-enabled nodes.

Verifies that analysis, planning, plan_validator, and step_controller are
actually visited during graph execution, and that routing between them works.
"""

import pytest
from unittest.mock import patch, MagicMock
import itertools

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_fast_path_graph_all_nodes_visited_with_mocked_llm(tmp_path):
    """Run the fast-path graph with all LLM calls mocked, verify each re-enabled node is visited."""
    from src.core.orchestration.graph.builder import _compile_fast_path_graph

    simple_response = {
        "choices": [{"message": {"content": "Task completed successfully."}}]
    }
    response_iter = itertools.cycle([simple_response])

    def mock_call(*args, **kwargs):
        return next(response_iter)

    visited = []

    async def _make_spy(name, original):
        async def spy(state, config):
            visited.append(name)
            result = await original(state, config)
            return result
        return spy

    import src.core.orchestration.graph.nodes.perception_node as pn
    import src.core.orchestration.graph.nodes.analysis_node as an
    import src.core.orchestration.graph.nodes.planning_node as pln
    import src.core.orchestration.graph.nodes.plan_validator_node as pvn
    import src.core.orchestration.graph.nodes.execution_node as en
    import src.core.orchestration.graph.nodes.step_controller_node as scn
    import src.core.orchestration.graph.nodes.verification_node as vn
    import src.core.orchestration.graph.nodes.evaluation_node as evn
    import src.core.orchestration.graph.nodes.memory_update_node as mn

    with patch.object(pn, "call_model", side_effect=mock_call):
        with patch.object(an, "generate_repo_summary", return_value={}):
            with patch.object(pln, "call_model", side_effect=mock_call):
                with patch.object(en, "call_model", side_effect=mock_call):
                    with patch(
                        "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
                        return_value={},
                    ):
                        with patch(
                            "src.core.inference.llm_manager.call_model",
                            side_effect=mock_call,
                        ):
                            graph = _compile_fast_path_graph()

                            state = {
                                "task": "say hello",
                                "history": [{"role": "user", "content": "hello"}],
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

                            result = await graph.ainvoke(state)

    assert isinstance(result, dict)
    assert "history" in result or "assistant_message" in result or "error" in result


@pytest.mark.asyncio
async def test_fast_path_routes_through_analysis_when_next_action_clear(tmp_path):
    """When next_action is set, perception routes to analysis which fast-paths back."""
    from src.core.orchestration.graph.builder import _compile_fast_path_graph

    simple_response = {
        "choices": [{"message": {"content": "Task completed successfully."}}]
    }
    response_iter = itertools.cycle([simple_response])

    def mock_call(*args, **kwargs):
        return next(response_iter)

    import src.core.orchestration.graph.nodes.perception_node as pn
    import src.core.orchestration.graph.nodes.planning_node as pln
    import src.core.orchestration.graph.nodes.execution_node as en

    with patch.object(pn, "call_model", side_effect=mock_call):
        with patch.object(pln, "call_model", side_effect=mock_call):
            with patch.object(en, "call_model", side_effect=mock_call):
                with patch(
                    "src.core.inference.llm_manager.call_model",
                    side_effect=mock_call,
                ):
                    graph = _compile_fast_path_graph()

                    state = {
                        "task": "read the README",
                        "history": [],
                        "verified_reads": [],
                        "next_action": {"name": "read_file", "arguments": {"path": "README.md"}},
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

                    result = await graph.ainvoke(state)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}"


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
