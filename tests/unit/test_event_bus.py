from src.core.orchestration.event_bus import EventBus, AgentMessage, MessagePriority


def test_event_bus_basic_subscribe_publish():
    bus = EventBus()
    received = []
    bus.subscribe("test_event", lambda p: received.append(p))
    bus.publish("test_event", {"data": "hello"})
    assert len(received) == 1
    assert received[0]["data"] == "hello"


def test_event_bus_unsubscribe():
    bus = EventBus()
    received = []

    def handler(p):
        received.append(p)

    bus.subscribe("test_event", handler)
    bus.publish("test_event", "first")
    bus.unsubscribe("test_event", handler)
    bus.publish("test_event", "second")
    assert len(received) == 1


def test_event_bus_agent_subscribe():
    bus = EventBus()
    received = []
    bus.subscribe_to_agent("agent1", lambda m: received.append(m))
    bus.publish_to_agent("agent1", {"task": "test"})
    assert len(received) == 1
    assert received[0].agent_id == "agent1"
    assert received[0].payload["task"] == "test"


def test_event_bus_agent_priority():
    bus = EventBus()
    low_received = []
    high_received = []
    bus.subscribe_to_agent(
        "agent1",
        lambda m: (
            low_received.append(m)
            if m.priority == MessagePriority.LOW
            else high_received.append(m)
        ),
    )
    bus.publish_to_agent("agent1", "low", priority=MessagePriority.LOW)
    bus.publish_to_agent("agent1", "high", priority=MessagePriority.HIGH)
    assert len(low_received) == 1
    assert len(high_received) == 1


def test_event_bus_broadcast():
    bus = EventBus()
    received1 = []
    received2 = []
    bus.subscribe_to_agent("agent1", lambda m: received1.append(m))
    bus.subscribe_to_agent("agent2", lambda m: received2.append(m))
    bus.broadcast_to_agents({"broadcast": "message"})
    assert len(received1) == 1
    assert len(received2) == 1


def test_event_bus_wildcard_subscriber():
    bus = EventBus()
    received = []
    bus.subscribe_to_agent("*", lambda m: received.append(m))
    bus.publish_to_agent("agent1", "msg1")
    bus.publish_to_agent("agent2", "msg2")
    assert len(received) == 2


def test_event_bus_list_agents():
    bus = EventBus()
    bus.subscribe_to_agent("agent1", lambda m: None)
    bus.subscribe_to_agent("agent2", lambda m: None)
    agents = bus.list_registered_agents()
    assert "agent1" in agents
    assert "agent2" in agents


def test_agent_message_properties():
    msg = AgentMessage(
        agent_id="test",
        payload={"key": "value"},
        priority=MessagePriority.HIGH,
        reply_to="parent",
    )
    assert msg.agent_id == "test"
    assert msg.payload["key"] == "value"
    assert msg.priority == MessagePriority.HIGH
    assert msg.reply_to == "parent"


def test_event_bus_reply_to():
    bus = EventBus()
    received = []
    bus.subscribe_to_agent("agent1", lambda m: received.append(m))
    msg = AgentMessage(agent_id="agent1", payload="message", reply_to="parent_agent")
    assert msg.reply_to == "parent_agent"
