from unittest.mock import MagicMock
from tui.tui_src.ui._bridge_tools import BridgeToolsMixin
from tui.tui_src.ui.bus import ToolCallFinishEvent, ToolCallErrorEvent


class _MockBridge(BridgeToolsMixin):
    def __init__(self):
        self._posted = []

    def _post(self, event):
        self._posted.append(event)


def test_bridge_strips_ansi_codes_on_tool_finish():
    bridge = _MockBridge()
    payload = {
        "title": "bash",
        "tool_call_id": "call_1",
        "result_formatted": "\x1b[32mSuccess\x1b[0m: \x1b[1mDone\x1b[0m\x1b[2K",
        "ok": True,
    }
    bridge._on_tool_finish(payload)

    assert len(bridge._posted) == 1
    event = bridge._posted[0]
    assert isinstance(event, ToolCallFinishEvent)
    assert "\x1b" not in event.result_text
    assert "Success: Done" in event.result_text


def test_bridge_truncates_huge_tool_output():
    bridge = _MockBridge()
    huge_str = "x" * 150_000
    payload = {
        "title": "cat",
        "tool_call_id": "call_2",
        "result_formatted": huge_str,
        "ok": True,
    }
    bridge._on_tool_finish(payload)

    assert len(bridge._posted) == 1
    event = bridge._posted[0]
    assert isinstance(event, ToolCallFinishEvent)
    assert len(event.result_text) < 110_000
    assert "characters truncated for display" in event.result_text


def test_bridge_strips_ansi_and_truncates_tool_error():
    bridge = _MockBridge()
    huge_err = "\x1b[31mError:\x1b[0m " + ("e" * 120_000)
    payload = {
        "title": "compiler",
        "tool_call_id": "call_3",
        "error": huge_err,
    }
    bridge._on_tool_error(payload)

    assert len(bridge._posted) == 1
    event = bridge._posted[0]
    assert isinstance(event, ToolCallErrorEvent)
    assert "\x1b" not in event.error
    assert len(event.error) < 110_000
    assert "characters truncated" in event.error
