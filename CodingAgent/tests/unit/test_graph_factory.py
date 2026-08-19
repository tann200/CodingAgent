from unittest.mock import patch

from src.core.orchestration.graph_factory import GraphFactory


def test_graph_factory_graph_types():
    assert GraphFactory.GRAPH_TYPES["planner"] == "planning"
    assert GraphFactory.GRAPH_TYPES["coder"] == "execution"
    assert GraphFactory.GRAPH_TYPES["reviewer"] == "verification"
    assert GraphFactory.GRAPH_TYPES["researcher"] == "search"


def test_graph_factory_get_graph_planner():
    graph = GraphFactory.get_graph("planner")
    assert graph is not None


def test_graph_factory_get_graph_coder():
    graph = GraphFactory.get_graph("coder")
    assert graph is not None


def test_graph_factory_get_graph_reviewer():
    graph = GraphFactory.get_graph("reviewer")
    assert graph is not None


def test_graph_factory_get_graph_researcher():
    graph = GraphFactory.get_graph("researcher")
    assert graph is not None


def test_graph_factory_invalid_role():
    graph = GraphFactory.get_graph("invalid_role")
    assert graph is None


def test_graph_factory_default_graph():
    graph = GraphFactory.get_default_graph()
    assert graph is not None


def test_graph_factory_get_graph_uses_tier_aware_selector_for_valid_role():
    with patch(
        "src.core.orchestration.graph.builder.get_compiled_graph_for_orchestrator",
        return_value="tier-graph",
    ) as mock_selector:
        graph = GraphFactory.get_graph("coder", model="gpt-4o-mini")

    assert graph == "tier-graph"
    mock_selector.assert_called_once_with(orchestrator=None, model="gpt-4o-mini")
