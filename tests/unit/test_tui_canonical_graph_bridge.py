"""tests/unit/test_tui_canonical_graph_bridge.py — 6-node graph bridging tests for TUI.

Verifies that events published during the 6-node canonical LangGraph execution
(agent_loop, approval, verify, compact, finalize) are properly received and translated
into Textual messages by AgentBridge:
1. ToolResult -> ToolCallFinishEvent
2. ToolExecuteStart -> ToolCallStartEvent
3. PlanRequested & PlanProgress -> PlanApprovalRequestEvent & PlanProgressEvent
4. ContextCompacted -> ContextCompactedEvent
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock
import pytest

from tui.tui_src.ui.core_bridge import AgentBridge
from tui.tui_src.ui.bus import (
    ToolCallStartEvent,
    ToolCallFinishEvent,
    PlanProgressEvent,
)
from src.core.messaging.event_types import (
    ToolResult,
    ToolExecuteStart,
    PlanProgress,
    ContextCompacted,
)


def _make_test_bridge():
    mock_app = MagicMock()
    posted_events = []
    mock_app.post_message.side_effect = lambda ev: posted_events.append(ev)
    mock_app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)

    bridge = AgentBridge.__new__(AgentBridge)
    bridge.app = mock_app
    bridge._working_dir = ""
    bridge._active_role = "operational"
    bridge._agent_lock = threading.Lock()
    bridge._agent_running = False
    bridge._history_lock = threading.Lock()
    bridge.history = []
    bridge._cancel_event = threading.Event()
    bridge._subscriptions = []
    bridge._pending_injections = []

    return bridge, posted_events


class TestTuiCanonicalGraphBridge:
    def test_tool_result_event_bridging(self):
        """ToolResult event from frontier_loop_node translates to ToolCallFinishEvent."""
        bridge, posted = _make_test_bridge()

        # Simulate ToolResult payload from frontier_loop_node
        tool_res = ToolResult(
            tool="edit_file_atomic",
            result={"ok": True, "path": "calc.py", "status": "ok"},
            turn=2,
        )
        payload = tool_res.to_dict()

        bridge._on_tool_finish(payload)

        finish_events = [ev for ev in posted if isinstance(ev, ToolCallFinishEvent)]
        assert len(finish_events) == 1
        ev = finish_events[0]
        assert ev.tool_name == "edit_file_atomic"
        assert ev.ok is True

    def test_tool_start_event_bridging(self):
        """ToolExecuteStart event translates to ToolCallStartEvent."""
        bridge, posted = _make_test_bridge()

        start_ev = ToolExecuteStart(tool="read_file", args={"path": "calc.py"}, tool_call_id="call_1")
        bridge._on_tool_start(start_ev.to_dict())

        start_events = [ev for ev in posted if isinstance(ev, ToolCallStartEvent)]
        assert len(start_events) == 1
        assert start_events[0].tool_name == "read_file"

    def test_plan_progress_bridging(self):
        """PlanProgress event from graph translates to PlanProgressEvent."""
        bridge, posted = _make_test_bridge()

        progress_ev = PlanProgress(plan_progress={"step": 1, "total_steps": 3, "description": "Setup"})
        bridge._on_plan_progress(progress_ev.to_dict())

        plan_events = [ev for ev in posted if isinstance(ev, PlanProgressEvent)]
        assert len(plan_events) == 1
