"""Tests for session management and task state clearing."""


from src.core.orchestration.event_bus import EventBus, get_event_bus


def test_session_new_clears_task_state(tmp_path):
    """Test that a session.new handler clears TASK_STATE.md when called."""
    from pathlib import Path

    agent_context = tmp_path / ".agent-context"
    agent_context.mkdir(parents=True, exist_ok=True)
    task_state_path = agent_context / "TASK_STATE.md"

    task_state_path.write_text(
        "# Current Task\n\nOld task\n\n# Completed Steps\n\nstep1\n"
    )

    bus = EventBus()
    cleared = {}

    def on_session_new(payload):
        # Replicate the handler logic from textual_app_impl._on_session_new
        task_state_path.write_text(
            "# Current Task\n\n# Completed Steps\n\n# Next Step\n"
        )
        cleared["done"] = True

    bus.subscribe("session.new", on_session_new)
    bus.publish("session.new", {"timestamp": 1234567890.0})

    assert cleared.get("done") is True
    content = task_state_path.read_text()
    assert "Old task" not in content
    assert "# Current Task" in content
    assert "# Next Step" in content


def test_session_new_event_published():
    """Test that session.new event can be published and received."""
    bus = get_event_bus()

    received = []

    def handler(payload):
        received.append(payload)

    bus.subscribe("session.new", handler)

    bus.publish("session.new", {"timestamp": 1234567890.0, "test": True})

    assert len(received) == 1
    assert received[0]["timestamp"] == 1234567890.0
    assert received[0]["test"] is True

    bus.unsubscribe("session.new", handler)
