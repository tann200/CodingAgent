from unittest.mock import MagicMock
from tui.tui_src.ui._bridge_tools import BridgeToolsMixin
from tui.tui_src.ui.bus import PlanRequestedEvent
from tui.tui_src.ui.components.chat_mixin import ChatDisplayMixin


class DummyBridge(BridgeToolsMixin):
    def __init__(self):
        self._posted_events = []

    def _post(self, event):
        self._posted_events.append(event)


def test_bridge_on_plan_requested_formats_list():
    bridge = DummyBridge()
    payload = {
        "plan": [
            {"description": "Step 1: check files"},
            {"description": "Step 2: apply patch"},
        ]
    }
    bridge._on_plan_requested(payload)

    assert len(bridge._posted_events) == 1
    event = bridge._posted_events[0]
    assert isinstance(event, PlanRequestedEvent)
    assert "Step 1: check files" in event.plan_text
    assert "Step 2: apply patch" in event.plan_text


def test_bridge_on_plan_requested_preserves_string():
    bridge = DummyBridge()
    payload = {"plan": "1. Do this\n2. Do that"}
    bridge._on_plan_requested(payload)

    assert len(bridge._posted_events) == 1
    event = bridge._posted_events[0]
    assert isinstance(event, PlanRequestedEvent)
    assert event.plan_text == "1. Do this\n2. Do that"


import pytest

@pytest.mark.asyncio
async def test_chat_handle_plan_requested_escapes_markup():
    class DummyChatLog:
        def __init__(self):
            self.mounted = []

        async def mount(self, w):
            self.mounted.append(w)

    class DummyChatApp(ChatDisplayMixin):
        def __init__(self):
            self.mounted = []
            self._current_stream = None
            self._chat_log = DummyChatLog()

        def query_one(self, selector, expected_type=None):
            if selector == "#chat_log":
                return self._chat_log
            raise KeyError(selector)

        async def _mount_chat_widget(self, w):
            self.mounted.append(w)

    app = DummyChatApp()
    # Test plan containing bracket characters that would cause Rich markup error if unescaped
    event = PlanRequestedEvent(plan_text="Plan step: list[str] and data[0]")
    await app._chat_handle_plan_requested(event)

    assert len(app._chat_log.mounted) == 2
    plan_widget = app._chat_log.mounted[0]
    # Check that the mounted Plan display widget received escaped plan text without crashing
    rendered_text = str(plan_widget.render())
    assert "list[str]" in rendered_text or "list\\[str\\]" in rendered_text
