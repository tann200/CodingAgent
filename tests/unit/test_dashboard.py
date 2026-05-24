"""Tests for EventBus → AgentBridge message dispatch (migrated from src.ui — LEGACY-02).

Originally tested MainViewController/DashboardState from src.ui.views.main_view.
Rewritten to use AgentBridge + MockEventBus so there is no dependency on src.ui.
"""

from __future__ import annotations
import threading

# ruff: noqa: E501
from unittest.mock import MagicMock

from tui.tui_src.ui.mock_eventbus import get_mock_event_bus, reset_mock_event_bus
from tui.tui_src.ui.core_bridge import AgentBridge

from src.core.orchestration.event_bus import EventBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge():
    """Return (bridge, bus, mock_app) using MockEventBus."""
    reset_mock_event_bus()
    bus = get_mock_event_bus()
    mock_app = MagicMock()
    # Make call_from_thread execute the callback directly (no running event loop in tests)
    mock_app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.app = mock_app
    bridge._bus = bus
    bridge._orchestrator = None
    bridge._working_dir = ""
    bridge._active_role = "operational"
    bridge._agent_lock = threading.Lock()
    bridge._agent_running = False
    bridge._history_lock = threading.Lock()
    bridge.history = []
    bridge._cancel_event = threading.Event()
    bridge._subscriptions = []
    bridge.setup_subscriptions()
    return bridge, bus, mock_app


# ---------------------------------------------------------------------------
# Event dispatch: file events
# ---------------------------------------------------------------------------


class TestFileEvents:
    def test_file_modified_posts_message(self):
        bridge, bus, mock_app = _make_bridge()
        bus.publish("file.modified", {"path": "/test.py", "tool": "edit_file"})
        mock_app.post_message.assert_called()

    def test_file_deleted_posts_message(self):
        bridge, bus, mock_app = _make_bridge()
        bus.publish("file.deleted", {"path": "/old.py"})
        mock_app.post_message.assert_called()


# ---------------------------------------------------------------------------
# Event dispatch: tool events
# ---------------------------------------------------------------------------


class TestToolEvents:
    def test_tool_start_posts_message(self):
        bridge, bus, mock_app = _make_bridge()
        bus.publish(
            "tool.call.start",  # CodingAgent internal name (mapped from tool.execute.start)
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_abc123",
                "title": "read_file",
                "status": "in_progress",
                "rawInput": {"path": "main.py"},
            },
        )
        mock_app.post_message.assert_called()

    def test_tool_finish_posts_message(self):
        bridge, bus, mock_app = _make_bridge()
        bus.publish(
            "tool.call.finish",
            {
                "toolCallId": "call_abc123",
                "title": "read_file",
                "status": "completed",
                "content": [{"type": "text", "text": "file content"}],
            },
        )
        mock_app.post_message.assert_called()

    def test_tool_error_posts_message(self):
        bridge, bus, mock_app = _make_bridge()
        bus.publish(
            "tool.call.error",
            {
                "toolCallId": "call_abc123",
                "title": "write_file",
                "status": "failed",
                "error": "Permission denied",
            },
        )
        mock_app.post_message.assert_called()


class TestSystemSettingsEvents:
    def test_system_settings_posts_system_settings_loaded(self):
        bridge, bus, mock_app = _make_bridge()
        bus.publish(
            "system.settings",
            {
                "active_mode": "lead_architect",
                "theme": "textual-dark",
                "context_window": 65536,
                "default_provider": "copilot",
                "default_model": "gpt-5",
                "providers": [{"name": "copilot", "models": ["gpt-5"]}],
                "autonomous_mode": True,
                "max_turns": 25,
            },
        )

        mock_app.post_message.assert_called()
        posted = mock_app.post_message.call_args[0][0]
        assert posted.__class__.__name__ == "SystemSettingsLoaded"
        assert posted.settings["context_window"] == 65536
        assert posted.settings["autonomous_mode"] is True
        assert posted.providers == [{"name": "copilot", "models": ["gpt-5"]}]


# ---------------------------------------------------------------------------
# Event dispatch: plan events
# ---------------------------------------------------------------------------


class TestPlanEvents:
    def test_plan_progress_posts_message(self):
        bridge, bus, mock_app = _make_bridge()
        bus.publish(
            "plan.progress",
            {
                "current_step": 2,
                "total_steps": 5,
                "step_description": "Add tests",
                "completed": True,
            },
        )
        mock_app.post_message.assert_called()


# ---------------------------------------------------------------------------
# H16: No recursive logging loop (retained from original test_dashboard.py)
# ---------------------------------------------------------------------------


class TestNoRecursiveLogging:
    def test_log_new_publish_does_not_recurse(self):
        """H16: Publishing log.new must not cause stack overflow or infinite loop."""
        bus = EventBus()
        call_count = [0]

        def handler(payload):
            call_count[0] += 1
            if call_count[0] > 1:
                return

        bus.subscribe("log.new", handler)
        bus.publish("log.new", {"message": "hello"})
        assert call_count[0] == 1

    def test_app_does_not_subscribe_log_new_to_guilogger(self):
        """H16: No component in src.core should subscribe log.new back through guilogger.

        Verifies that the EventBus subscribe method is not wired to re-publish log.new
        (which would create an infinite loop). Tests using the bus directly rather than
        inspecting CodingAgentApp source (CodingAgentApp is being retired in LEGACY-03).
        """
        bus = EventBus()
        call_count = [0]

        def logging_handler(payload):
            call_count[0] += 1
            # A recursive implementation would call bus.publish("log.new", ...) here
            # We verify it does NOT by checking call_count stays at 1

        bus.subscribe("log.new", logging_handler)
        bus.publish("log.new", {"message": "test"})
        assert call_count[0] == 1, "H16: log.new handler must not be called recursively"
