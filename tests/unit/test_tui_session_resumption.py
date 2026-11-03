"""Unit tests for SPEC-P1.5: TUI session resumption conversation history preservation."""

from unittest.mock import MagicMock
from tui.tui_src.ui._bridge_session import BridgeSessionMixin


class MockMessageManager:
    def __init__(self):
        self.messages = []

    def clear(self):
        self.messages.clear()

    def append(self, role, content):
        self.messages.append({"role": role, "content": content})


class DummyBridge(BridgeSessionMixin):
    def __init__(self, orch=None):
        self._history_lock = MagicMock(__enter__=MagicMock(), __exit__=MagicMock())
        self.history = []
        self._orchestrator = orch
        self.working_dir = ""
        self.published_events = []

    def publish_typed(self, event):
        self.published_events.append(event)

    def publish(self, name, payload):
        self.published_events.append((name, payload))


def test_resume_session_hydrates_messages_and_does_not_clear():
    """SPEC-P1.5: resume_session preserves messages in bridge history and orchestrator message manager."""
    msg_mgr = MockMessageManager()
    orch = MagicMock()
    orch.msg_mgr = msg_mgr
    orch.start_new_task = MagicMock()

    bridge = DummyBridge(orch=orch)
    messages = [
        {"role": "user", "content": "Initial prompt"},
        {"role": "assistant", "content": "Assistant answer"},
    ]

    bridge.resume_session(session_id="sess-123", messages=messages, working_dir="/test/dir")

    # Historical messages populated in bridge
    assert bridge.history == [("user", "Initial prompt"), ("assistant", "Assistant answer")]
    assert bridge.working_dir == "/test/dir"

    # Message manager in orchestrator hydrated
    assert len(msg_mgr.messages) == 2
    assert msg_mgr.messages[0] == {"role": "user", "content": "Initial prompt"}
    assert msg_mgr.messages[1] == {"role": "assistant", "content": "Assistant answer"}

    # start_new_task must NOT be called on resume
    orch.start_new_task.assert_not_called()

    # SessionHydrated event published
    assert len(bridge.published_events) == 1
    ev = bridge.published_events[0]
    assert getattr(ev, "session_id", None) == "sess-123"
    assert getattr(ev, "working_dir", None) == "/test/dir"
