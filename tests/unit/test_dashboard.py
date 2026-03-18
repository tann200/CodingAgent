"""Tests for the MainViewController dashboard functionality and EventBus integration."""

import pytest
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
        controller = MainViewController(bus)

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

        bus.publish(
            "tool.execute.start", {"tool": "read_file", "args": {"path": "main.py"}}
        )

        assert len(controller.dashboard.tool_activity) == 1
        assert controller.dashboard.tool_activity[0]["tool"] == "read_file"
        assert controller.dashboard.tool_activity[0]["status"] == "running"

    def test_on_tool_finish(self):
        """Test tool.execute.finish event updates tool activity."""
        bus = EventBus()
        controller = MainViewController(bus)

        bus.publish("tool.execute.start", {"tool": "read_file", "args": {}})
        bus.publish("tool.execute.finish", {"tool": "read_file", "ok": True})

        activity = controller.dashboard.tool_activity[0]
        assert activity["status"] == "ok"

    def test_on_tool_error(self):
        """Test tool.execute.error event updates tool activity."""
        bus = EventBus()
        controller = MainViewController(bus)

        bus.publish("tool.execute.start", {"tool": "write_file", "args": {}})
        bus.publish(
            "tool.execute.error", {"tool": "write_file", "error": "Permission denied"}
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
        bus.publish("tool.execute.start", {"tool": "read_file", "args": {}})

        summary = controller.get_dashboard_summary()

        assert summary["modified_files_count"] == 1
        assert "/a.py" in summary["modified_files"]
        assert len(summary["recent_activities"]) == 1

    def test_tool_activity_limit(self):
        """Test that tool activity is limited to 10 items."""
        bus = EventBus()
        controller = MainViewController(bus)

        # Publish 12 tool start events
        for i in range(12):
            bus.publish("tool.execute.start", {"tool": f"tool_{i}", "args": {}})

        # Should only keep last 10
        assert len(controller.dashboard.tool_activity) == 10
