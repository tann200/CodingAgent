from src.core.orchestration.orchestrator import Orchestrator
from unittest.mock import patch
import itertools


def test_orchestrator_instantiation(tmp_path):
    """Test that the orchestrator can be instantiated."""
    orch = Orchestrator(working_dir=str(tmp_path))
    assert orch is not None
    assert orch.msg_mgr is not None


def test_orchestrator_graph_compiles(tmp_path):
    """Test that the LangGraph can be compiled."""
    # Just verify the graph can be compiled
    from src.core.orchestration.graph.builder import compile_agent_graph

    graph = compile_agent_graph()
    assert graph is not None


def test_orchestrator_run_with_mocked_llm(tmp_path):
    """Test that orchestrator can run with a mocked LLM that returns a simple response."""
    orch = Orchestrator(working_dir=str(tmp_path))

    # Create a simple mock that just returns a completion message
    simple_response = {
        "choices": [{"message": {"content": "Task completed successfully."}}]
    }
    response_iter = itertools.cycle([simple_response])

    def mock_call(*args, **kwargs):
        return next(response_iter)

    # Patch call_model
    with patch(
        "src.core.orchestration.graph.nodes.perception_node.call_model",
        side_effect=mock_call,
    ):
        with patch(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            side_effect=mock_call,
        ):
            with patch(
                "src.core.orchestration.graph.nodes.execution_node.call_model",
                side_effect=mock_call,
            ):
                with patch(
                    "src.core.orchestration.graph.nodes.debug_node.call_model",
                    side_effect=mock_call,
                ):
                    with patch(
                        "src.core.orchestration.graph.nodes.replan_node.call_model",
                        side_effect=mock_call,
                    ):
                        with patch(
                            "src.core.inference.llm_manager.call_model",
                            side_effect=mock_call,
                        ):
                            # Execute with a simple task - should not raise an exception
                            res = orch.run_agent_once(
                                None, [{"role": "user", "content": "hello"}], {}
                            )

                            # Result should be a dict with some response
                            assert isinstance(res, dict)
                            assert "error" in res or "assistant_message" in res
