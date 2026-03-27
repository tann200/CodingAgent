"""
Unit tests for session_registry.py - Session Registry
"""

import pytest
import time
from src.core.orchestration.session_registry import (
    SessionRegistry,
    SessionInfo,
    SessionStatus,
    SessionPriority,
    get_session_registry,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset registry singleton before each test."""
    SessionRegistry.reset_instance()
    yield
    SessionRegistry.reset_instance()


class TestSessionInfo:
    def test_initialization(self):
        info = SessionInfo(
            session_id="test-123",
            task_id="task-456",
            role="operational",
            status=SessionStatus.RUNNING,
            priority=SessionPriority.NORMAL,
            created_at=time.time(),
            last_active_at=time.time(),
        )
        assert info.session_id == "test-123"
        assert info.role == "operational"
        assert info.status == SessionStatus.RUNNING
        assert info.priority == SessionPriority.NORMAL
        assert info.child_session_ids == []

    def test_child_sessions_default_empty(self):
        info = SessionInfo(
            session_id="test-123",
            task_id="task-456",
            role="operational",
            status=SessionStatus.RUNNING,
            priority=SessionPriority.NORMAL,
            created_at=time.time(),
            last_active_at=time.time(),
        )
        assert info.child_session_ids == []


class TestSessionRegistry:
    def test_singleton(self):
        registry1 = SessionRegistry.get_instance()
        registry2 = SessionRegistry.get_instance()
        assert registry1 is registry2

    def test_register_session(self):
        registry = SessionRegistry.get_instance()
        info = registry.register_session(
            session_id="session-1",
            role="operational",
            task_description="Test task",
        )
        assert info.session_id == "session-1"
        assert info.role == "operational"
        assert info.status == SessionStatus.INITIALIZING

    def test_register_session_with_parent(self):
        registry = SessionRegistry.get_instance()
        parent = registry.register_session(session_id="parent-1", role="operational")
        child = registry.register_session(
            session_id="child-1",
            role="analyst",
            parent_session_id="parent-1",
        )
        assert child.parent_session_id == "parent-1"
        assert "child-1" in parent.child_session_ids

    def test_register_duplicate_returns_existing(self):
        registry = SessionRegistry.get_instance()
        info1 = registry.register_session(session_id="session-1")
        info2 = registry.register_session(session_id="session-1")
        assert info1 is info2

    def test_unregister_session(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        result = registry.unregister_session("session-1", "Test cleanup")
        assert result is True
        assert registry.get_session("session-1") is None

    def test_unregister_nonexistent_returns_false(self):
        registry = SessionRegistry.get_instance()
        result = registry.unregister_session("nonexistent")
        assert result is False

    def test_unregister_cascades_to_children(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="parent-1")
        registry.register_session(session_id="child-1", parent_session_id="parent-1")
        registry.unregister_session("parent-1", "Test cascade")
        assert registry.get_session("child-1") is None

    def test_update_session_status(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        result = registry.update_session_status("session-1", SessionStatus.RUNNING)
        assert result is True
        info = registry.get_session("session-1")
        assert info.status == SessionStatus.RUNNING

    def test_update_session_status_not_found(self):
        registry = SessionRegistry.get_instance()
        result = registry.update_session_status("nonexistent", SessionStatus.RUNNING)
        assert result is False

    def test_update_session_activity(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        result = registry.update_session_activity(
            "session-1", tool_call=True, token_usage=100
        )
        assert result is True
        info = registry.get_session("session-1")
        assert info.tool_call_count == 1
        assert info.token_usage == 100

    def test_get_active_sessions(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        registry.register_session(session_id="session-2")
        registry.update_session_status("session-1", SessionStatus.COMPLETED)

        active = registry.get_active_sessions()
        assert len(active) == 1
        assert active[0].session_id == "session-2"

    def test_get_active_sessions_filter_by_role(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1", role="operational")
        registry.register_session(session_id="session-2", role="analyst")

        operational = registry.get_active_sessions(role="operational")
        assert len(operational) == 1
        assert operational[0].session_id == "session-1"

    def test_get_active_sessions_filter_by_status(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        registry.register_session(session_id="session-2")
        registry.update_session_status("session-1", SessionStatus.WAITING)

        waiting = registry.get_active_sessions(status=SessionStatus.WAITING)
        assert len(waiting) == 1
        assert waiting[0].session_id == "session-1"

    def test_get_session_tree(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="parent-1")
        registry.register_session(session_id="child-1", parent_session_id="parent-1")
        registry.register_session(
            session_id="grandchild-1", parent_session_id="child-1"
        )

        tree = registry.get_session_tree("parent-1")
        assert tree["session_id"] == "parent-1"
        assert len(tree["children"]) == 1
        assert tree["children"][0]["session_id"] == "child-1"
        assert len(tree["children"][0]["children"]) == 1

    def test_get_stale_sessions(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        registry.register_session(session_id="session-2")
        # Transition to running so they're not excluded
        registry.update_session_status("session-1", SessionStatus.RUNNING)
        registry.update_session_status("session-2", SessionStatus.RUNNING)
        # session-1 is stale (mock by setting old timestamp)
        registry._sessions["session-1"].last_active_at = time.time() - 400

        stale = registry.get_stale_sessions(threshold=300)
        assert len(stale) == 1
        assert stale[0].session_id == "session-1"

    def test_get_statistics(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        registry.register_session(session_id="session-2", role="analyst")
        registry.update_session_status("session-1", SessionStatus.RUNNING)
        registry.update_session_status("session-2", SessionStatus.RUNNING)
        registry.update_session_activity("session-1", tool_call=True, token_usage=100)

        stats = registry.get_statistics()
        assert stats["total_sessions"] == 2
        assert stats["by_status"]["running"] == 2
        assert stats["by_role"]["operational"] == 1
        assert stats["by_role"]["analyst"] == 1
        assert stats["total_tool_calls"] == 1
        assert stats["total_token_usage"] == 100

    def test_event_callbacks(self):
        registry = SessionRegistry.get_instance()
        registered_info = []
        unregistered_info = []
        status_changes = []

        registry.on_session_registered(lambda info: registered_info.append(info))
        registry.on_session_unregistered(lambda info: unregistered_info.append(info))
        registry.on_status_changed(
            lambda sid, old, new: status_changes.append((sid, old, new))
        )

        registry.register_session(session_id="session-1")
        registry.update_session_status("session-1", SessionStatus.RUNNING)
        registry.unregister_session("session-1")

        assert len(registered_info) == 1
        assert registered_info[0].session_id == "session-1"
        assert len(unregistered_info) == 1
        assert len(status_changes) == 1
        assert status_changes[0] == (
            "session-1",
            SessionStatus.INITIALIZING,
            SessionStatus.RUNNING,
        )

    def test_health_alert_callback(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        registry.update_session_status("session-1", SessionStatus.RUNNING)
        # Make session stale
        registry._sessions["session-1"].last_active_at = time.time() - 400

        alerts = []
        registry.on_health_alert(lambda sid, msg: alerts.append((sid, msg)))

        # Trigger health check
        stale = registry.get_stale_sessions()
        for info in stale:
            registry._notify_health_alert(
                info.session_id, f"Stale session {info.session_id}"
            )

        assert len(alerts) == 1
        assert alerts[0][0] == "session-1"

    def test_get_session(self):
        registry = SessionRegistry.get_instance()
        registry.register_session(session_id="session-1")
        info = registry.get_session("session-1")
        assert info is not None
        assert info.session_id == "session-1"

    def test_get_session_not_found(self):
        registry = SessionRegistry.get_instance()
        info = registry.get_session("nonexistent")
        assert info is None


def test_get_session_registry():
    """Test module-level getter."""
    SessionRegistry.reset_instance()
    registry = get_session_registry()
    assert registry is not None
    SessionRegistry.reset_instance()
