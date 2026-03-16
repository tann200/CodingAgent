"""Tests for session management and task state clearing."""

import tempfile
from pathlib import Path

from src.core.orchestration.event_bus import EventBus, get_event_bus


def test_session_new_clears_task_state(tmp_path):
    """Test that session.new event clears TASK_STATE.md."""
    agent_context = tmp_path / ".agent-context"
    agent_context.mkdir(parents=True, exist_ok=True)
    task_state_path = agent_context / "TASK_STATE.md"

    task_state_path.write_text(
        "# Current Task\n\nOld task\n\n# Completed Steps\n\nstep1\n"
    )

    bus = EventBus()

    received = {}

    def on_session_new(payload):
        received["payload"] = payload

    bus.subscribe("session.new", on_session_new)

    task_state_path_expected = tmp_path / ".agent-context" / "TASK_STATE.md"

    task_state_path_expected.write_text(
        "# Current Task\n\n# Completed Steps\n\n# Next Step\n"
    )

    bus.publish("session.new", {"timestamp": 1234567890.0})

    content = task_state_path_expected.read_text()
    assert "# Current Task" in content
    assert "# Completed Steps" in content
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
