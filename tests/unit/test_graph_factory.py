import pytest
from src.core.orchestration.graph_factory import GraphFactory, HubAndSpokeCoordinator
from src.core.orchestration.event_bus import EventBus


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


def test_hub_and_spoke_coordinator_init():
    coordinator = HubAndSpokeCoordinator()
    assert len(coordinator.agents) == 0
    assert len(coordinator.task_queue) == 0


def test_hub_and_spoke_register_agent():
    coordinator = HubAndSpokeCoordinator()
    assert coordinator.register_agent("planner1", "planner")
    assert "planner1" in coordinator.agents
    assert coordinator.agents["planner1"]["role"] == "planner"


def test_hub_and_spoke_register_invalid_role():
    coordinator = HubAndSpokeCoordinator()
    assert not coordinator.register_agent("agent1", "invalid_role")


def test_hub_and_spoke_dispatch_task():
    coordinator = HubAndSpokeCoordinator()
    coordinator.register_agent("planner1", "planner")
    coordinator.dispatch_task("Break down the task", "planner1", {"context": "test"})
    assert len(coordinator.task_queue) == 1
    assert coordinator.task_queue[0]["task"] == "Break down the task"
    assert coordinator.task_queue[0]["agent_id"] == "planner1"


def test_hub_and_spoke_list_agents():
    coordinator = HubAndSpokeCoordinator()
    coordinator.register_agent("planner1", "planner")
    coordinator.register_agent("coder1", "coder")
    agents = coordinator.list_agents()
    assert len(agents) == 2
    assert agents["planner1"]["role"] == "planner"
    assert agents["coder1"]["role"] == "coder"


def test_hub_and_spoke_get_agent_status():
    coordinator = HubAndSpokeCoordinator()
    coordinator.register_agent("planner1", "planner")
    status = coordinator.get_agent_status("planner1")
    assert status == "idle"


def test_hub_and_spoke_broadcast_message():
    bus = EventBus()
    coordinator = HubAndSpokeCoordinator(event_bus=bus)
    coordinator.register_agent("agent1", "planner")
    received = []
    bus.subscribe_to_agent("agent1", lambda m: received.append(m))
    coordinator.broadcast_message({"broadcast": "hello"})
    assert len(received) == 1


def test_hub_and_spoke_with_event_bus():
    bus = EventBus()
    coordinator = HubAndSpokeCoordinator(event_bus=bus)
    coordinator.register_agent("agent1", "planner")
    assert "agent1" in bus.list_registered_agents()
