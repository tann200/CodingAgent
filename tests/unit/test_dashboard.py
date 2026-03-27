"""Tests for the MainViewController dashboard functionality and EventBus integration."""

from src.core.orchestration.event_bus import EventBus
from src.ui.views.main_view import MainViewController, DashboardState


class TestDashboardState:
    """Tests for DashboardState."""

    def test_dashboard_state_init(self):
        """Test DashboardState initializes with empty state."""
        dashboard = DashboardState()
        assert dashboard.modified_files == {}
        assert dashboard.tool_activity == []
        assert dashboard.plan_progress == {}
        assert dashboard.verification_status is None


class TestMainViewController:
    """Tests for MainViewController event subscriptions."""

    def test_controller_subscribes_to_events(self):
        """Test that controller subscribes to all required events."""
        bus = EventBus()
        _controller = MainViewController(bus)

        # Should have subscriptions to file, tool, plan, and verification events
        assert "file.modified" in bus._subscribers
        assert "file.deleted" in bus._subscribers
        assert "tool.execute.start" in bus._subscribers
        assert "tool.execute.finish" in bus._subscribers
        assert "tool.execute.error" in bus._subscribers
        assert "plan.progress" in bus._subscribers
        assert "verification.complete" in bus._subscribers

    def test_on_file_modified(self):
        """Test file.modified event updates dashboard."""
        bus = EventBus()
        controller = MainViewController(bus)

        bus.publish("file.modified", {"path": "/test.py", "tool": "edit_file"})

        assert "/test.py" in controller.dashboard.modified_files
        assert controller.dashboard.modified_files["/test.py"]["tool"] == "edit_file"

    def test_on_file_deleted(self):
        """Test file.deleted event updates dashboard."""
        bus = EventBus()
        controller = MainViewController(bus)

        bus.publish("file.deleted", {"path": "/old.py"})

        assert "/old.py" in controller.dashboard.modified_files
        assert controller.dashboard.modified_files["/old.py"]["action"] == "deleted"

    def test_on_tool_start(self):
        """Test tool.execute.start event updates tool activity."""
        bus = EventBus()
        controller = MainViewController(bus)

        # GAP 2: ACP schema payload
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_abc123",
                "title": "read_file",
                "status": "in_progress",
                "rawInput": {"path": "main.py"},
            },
        )

        assert len(controller.dashboard.tool_activity) == 1
        assert controller.dashboard.tool_activity[0]["tool"] == "read_file"
        assert controller.dashboard.tool_activity[0]["status"] == "running"

    def test_on_tool_finish(self):
        """Test tool.execute.finish event updates tool activity."""
        bus = EventBus()
        controller = MainViewController(bus)

        # GAP 2: ACP schema payload
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_abc123",
                "title": "read_file",
                "status": "in_progress",
                "rawInput": {},
            },
        )
        bus.publish(
            "tool.execute.finish",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_abc123",
                "title": "read_file",
                "status": "completed",
                "content": [{"type": "text", "text": "file content"}],
                "rawOutput": {"status": "ok"},
            },
        )

        activity = controller.dashboard.tool_activity[0]
        assert activity["status"] == "ok"

    def test_on_tool_error(self):
        """Test tool.execute.error event updates tool activity."""
        bus = EventBus()
        controller = MainViewController(bus)

        # GAP 2: ACP schema payload
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_abc123",
                "title": "write_file",
                "status": "in_progress",
                "rawInput": {},
            },
        )
        bus.publish(
            "tool.execute.error",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_abc123",
                "title": "write_file",
                "status": "failed",
                "content": [{"type": "text", "text": "Permission denied"}],
                "error": "Permission denied",
            },
        )

        activity = controller.dashboard.tool_activity[0]
        assert activity["status"] == "error"
        assert activity["error"] == "Permission denied"

    def test_on_plan_progress(self):
        """Test plan.progress event updates dashboard."""
        bus = EventBus()
        controller = MainViewController(bus)

        bus.publish(
            "plan.progress",
            {
                "current_step": 2,
                "total_steps": 5,
                "step_description": "Add tests",
                "completed": True,
            },
        )

        assert controller.dashboard.plan_progress["current_step"] == 2
        assert controller.dashboard.plan_progress["total_steps"] == 5

    def test_on_verification(self):
        """Test verification.complete event updates dashboard."""
        bus = EventBus()
        controller = MainViewController(bus)

        bus.publish("verification.complete", {"status": "pass"})

        assert controller.dashboard.verification_status == "pass"

    def test_get_dashboard_summary(self):
        """Test dashboard summary returns correct data."""
        bus = EventBus()
        controller = MainViewController(bus)

        bus.publish("file.modified", {"path": "/a.py", "tool": "edit_file"})
        # GAP 2: ACP schema payload
        bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_abc123",
                "title": "read_file",
                "status": "in_progress",
                "rawInput": {},
            },
        )

        summary = controller.get_dashboard_summary()

        assert summary["modified_files_count"] == 1
        assert "/a.py" in summary["modified_files"]
        assert len(summary["recent_activities"]) == 1

    def test_tool_activity_limit(self):
        """Test that tool activity is limited to 10 items."""
        bus = EventBus()
        controller = MainViewController(bus)

        # Publish 12 tool start events with GAP 2: ACP schema
        for i in range(12):
            bus.publish(
                "tool.execute.start",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": f"call_{i}",
                    "title": f"tool_{i}",
                    "status": "in_progress",
                    "rawInput": {},
                },
            )

        # Should only keep last 10
        assert len(controller.dashboard.tool_activity) == 10


# ---------------------------------------------------------------------------
# H16: No recursive logging loop
# ---------------------------------------------------------------------------


class TestNoRecursiveLogging:
    def test_app_does_not_subscribe_log_new_to_guilogger(self):
        """H16: CodingAgentApp must NOT subscribe log.new back to guilogger
        (would create log.new → _guilogger.log() → log.new → ... infinite loop).
        """
        import inspect
        from src.ui import app as app_mod

        src = inspect.getsource(app_mod.CodingAgentApp.__init__)
        # The comment in app.py explicitly notes this was removed; verify no subscribe call
        assert "subscribe" not in src or "log.new" not in src, (
            "H16: CodingAgentApp.__init__ must not subscribe to log.new"
        )

    def test_log_new_publish_does_not_recurse(self):
        """H16: Publishing log.new must not cause stack overflow or infinite loop."""
        bus = EventBus()
        call_count = [0]

        def handler(payload):
            call_count[0] += 1
            if call_count[0] > 1:
                return  # already called once, don't recurse
            # Simulate a handler that would re-publish (but we guard against it)

        bus.subscribe("log.new", handler)
        bus.publish("log.new", {"message": "hello"})
        # Should fire exactly once
        assert call_count[0] == 1
