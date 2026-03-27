"""
Unit tests for session_watcher.py - Session Health Watcher
"""

import pytest
import time
from unittest.mock import MagicMock, patch

from src.core.orchestration.session_watcher import (
    SessionWatcher,
    HealthReport,
    HealthStatus,
    SessionAlert,
    AlertLevel,
    get_session_watcher,
)


@pytest.fixture(autouse=True)
def reset_watcher():
    """Reset watcher singleton before each test."""
    SessionWatcher.reset_instance()
    yield
    SessionWatcher.reset_instance()


class TestHealthReport:
    def test_initialization(self):
        report = HealthReport(
            session_id="test-123",
            status=HealthStatus.HEALTHY,
            last_active=time.time(),
            idle_time=10.0,
            tool_call_count=5,
            token_usage=1000,
            error_count=0,
        )
        assert report.session_id == "test-123"
        assert report.status == HealthStatus.HEALTHY
        assert report.issues == []
        assert report.recommendations == []


class TestSessionAlert:
    def test_initialization(self):
        alert = SessionAlert(
            alert_id="alert-123",
            session_id="session-1",
            level=AlertLevel.WARNING,
            title="Test Alert",
            message="Test message",
            timestamp=time.time(),
        )
        assert alert.alert_id == "alert-123"
        assert alert.level == AlertLevel.WARNING


class TestSessionWatcher:
    def test_singleton(self):
        watcher1 = SessionWatcher.get_instance()
        watcher2 = SessionWatcher.get_instance()
        assert watcher1 is watcher2

    def test_initialization(self):
        watcher = SessionWatcher(
            stale_threshold=100.0,
            critical_threshold=200.0,
            max_tool_calls=500,
            max_token_usage=50000,
            max_errors=5,
        )
        assert watcher.stale_threshold == 100.0
        assert watcher.critical_threshold == 200.0
        assert watcher.max_tool_calls == 500
        assert watcher.max_token_usage == 50000
        assert watcher.max_errors == 5

    def test_on_alert(self):
        watcher = SessionWatcher()
        received = []

        def handler(alert):
            received.append(alert)

        watcher.on_alert(handler)
        alert = SessionAlert(
            alert_id="test",
            session_id="s1",
            level=AlertLevel.WARNING,
            title="Test",
            message="Test",
            timestamp=time.time(),
        )
        watcher._emit_alert(alert)
        assert len(received) == 1

    def test_enable_auto_cleanup(self):
        watcher = SessionWatcher()
        watcher.enable_auto_cleanup(True)
        assert watcher._auto_cleanup_enabled is True
        watcher.enable_auto_cleanup(False)
        assert watcher._auto_cleanup_enabled is False

    def test_get_watcher_stats(self):
        watcher = SessionWatcher(
            stale_threshold=100.0,
            critical_threshold=200.0,
        )
        stats = watcher.get_watcher_stats()
        assert stats["stale_threshold"] == 100.0
        assert stats["critical_threshold"] == 200.0
        assert stats["running"] is False
        assert stats["auto_cleanup_enabled"] is False

    def test_get_recent_alerts(self):
        watcher = SessionWatcher()
        for i in range(5):
            alert = SessionAlert(
                alert_id=f"alert-{i}",
                session_id="s1",
                level=AlertLevel.WARNING,
                title=f"Alert {i}",
                message="Test",
                timestamp=time.time(),
            )
            watcher._emit_alert(alert)

        alerts = watcher.get_recent_alerts()
        assert len(alerts) == 5

    def test_get_recent_alerts_filter_by_session(self):
        watcher = SessionWatcher()
        for session_id in ["s1", "s1", "s2"]:
            alert = SessionAlert(
                alert_id=f"alert-{session_id}",
                session_id=session_id,
                level=AlertLevel.WARNING,
                title="Test",
                message="Test",
                timestamp=time.time(),
            )
            watcher._emit_alert(alert)

        alerts = watcher.get_recent_alerts(session_id="s1")
        assert len(alerts) == 2

    def test_get_recent_alerts_filter_by_level(self):
        watcher = SessionWatcher()
        for level in [AlertLevel.INFO, AlertLevel.WARNING, AlertLevel.WARNING]:
            alert = SessionAlert(
                alert_id=f"alert-{level.value}",
                session_id="s1",
                level=level,
                title="Test",
                message="Test",
                timestamp=time.time(),
            )
            watcher._emit_alert(alert)

        alerts = watcher.get_recent_alerts(level=AlertLevel.WARNING)
        assert len(alerts) == 2

    def test_get_recent_alerts_with_limit(self):
        watcher = SessionWatcher()
        for i in range(10):
            alert = SessionAlert(
                alert_id=f"alert-{i}",
                session_id="s1",
                level=AlertLevel.WARNING,
                title="Test",
                message="Test",
                timestamp=time.time(),
            )
            watcher._emit_alert(alert)

        alerts = watcher.get_recent_alerts(limit=5)
        assert len(alerts) == 5

    def test_check_session_health_healthy(self):
        watcher = SessionWatcher()
        mock_info = MagicMock()
        mock_info.session_id = "session-1"
        mock_info.last_active_at = time.time()
        mock_info.tool_call_count = 5
        mock_info.token_usage = 1000
        mock_info.error_count = 0

        report = watcher._check_session_health(mock_info)
        assert report.status == HealthStatus.HEALTHY
        assert len(report.issues) == 0

    def test_check_session_health_stale(self):
        watcher = SessionWatcher(stale_threshold=100.0, critical_threshold=200.0)
        mock_info = MagicMock()
        mock_info.session_id = "session-1"
        mock_info.last_active_at = time.time() - 150
        mock_info.tool_call_count = 5
        mock_info.token_usage = 1000
        mock_info.error_count = 0

        report = watcher._check_session_health(mock_info)
        assert report.status == HealthStatus.STALE
        assert len(report.issues) == 1

    def test_check_session_health_critical(self):
        watcher = SessionWatcher(stale_threshold=100.0, critical_threshold=200.0)
        mock_info = MagicMock()
        mock_info.session_id = "session-1"
        mock_info.last_active_at = time.time() - 250
        mock_info.tool_call_count = 5
        mock_info.token_usage = 1000
        mock_info.error_count = 0

        report = watcher._check_session_health(mock_info)
        assert report.status == HealthStatus.CRITICAL
        assert len(report.issues) == 1

    def test_check_session_health_high_tool_calls(self):
        watcher = SessionWatcher(max_tool_calls=100)
        mock_info = MagicMock()
        mock_info.session_id = "session-1"
        mock_info.last_active_at = time.time()
        mock_info.tool_call_count = 150
        mock_info.token_usage = 1000
        mock_info.error_count = 0

        report = watcher._check_session_health(mock_info)
        assert report.status == HealthStatus.WARNING
        assert len(report.issues) == 1

    def test_check_session_health_high_token_usage(self):
        watcher = SessionWatcher(max_token_usage=5000)
        mock_info = MagicMock()
        mock_info.session_id = "session-1"
        mock_info.last_active_at = time.time()
        mock_info.tool_call_count = 5
        mock_info.token_usage = 10000
        mock_info.error_count = 0

        report = watcher._check_session_health(mock_info)
        assert report.status == HealthStatus.WARNING
        assert len(report.issues) == 1

    def test_check_session_health_high_errors(self):
        watcher = SessionWatcher(max_errors=5)
        mock_info = MagicMock()
        mock_info.session_id = "session-1"
        mock_info.last_active_at = time.time()
        mock_info.tool_call_count = 5
        mock_info.token_usage = 1000
        mock_info.error_count = 10

        report = watcher._check_session_health(mock_info)
        assert report.status == HealthStatus.WARNING
        assert len(report.issues) == 1

    @pytest.mark.asyncio
    async def test_handle_health_report_healthy_no_alert(self):
        watcher = SessionWatcher()
        report = HealthReport(
            session_id="session-1",
            status=HealthStatus.HEALTHY,
            last_active=time.time(),
            idle_time=10.0,
            tool_call_count=5,
            token_usage=1000,
            error_count=0,
        )
        await watcher._handle_health_report(report)
        assert len(watcher._alert_history) == 0

    @pytest.mark.asyncio
    async def test_handle_health_report_stale_generates_alert(self):
        watcher = SessionWatcher()
        report = HealthReport(
            session_id="session-1",
            status=HealthStatus.STALE,
            last_active=time.time() - 150,
            idle_time=150.0,
            tool_call_count=5,
            token_usage=1000,
            error_count=0,
            issues=["Session stale for 150s"],
        )
        await watcher._handle_health_report(report)
        assert len(watcher._alert_history) == 1
        assert watcher._alert_history[0].level == AlertLevel.WARNING

    @pytest.mark.asyncio
    async def test_handle_health_report_critical_generates_alert(self):
        watcher = SessionWatcher()
        report = HealthReport(
            session_id="session-1",
            status=HealthStatus.CRITICAL,
            last_active=time.time() - 250,
            idle_time=250.0,
            tool_call_count=5,
            token_usage=1000,
            error_count=0,
            issues=["Session stale for 250s"],
        )
        await watcher._handle_health_report(report)
        assert len(watcher._alert_history) == 1
        assert watcher._alert_history[0].level == AlertLevel.CRITICAL

    @pytest.mark.asyncio
    async def test_cleanup_stale_session_child(self):
        watcher = SessionWatcher()
        mock_registry = MagicMock()
        mock_info = MagicMock()
        mock_info.session_id = "child-1"
        mock_info.parent_session_id = "parent-1"
        mock_registry.get_session.return_value = mock_info

        with patch(
            "src.core.orchestration.session_registry.get_session_registry",
            return_value=mock_registry,
        ):
            result = await watcher._cleanup_stale_session("child-1")
            assert result is True
            mock_registry.unregister_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_stale_session_parent_no_cleanup(self):
        watcher = SessionWatcher()
        mock_registry = MagicMock()
        mock_info = MagicMock()
        mock_info.session_id = "parent-1"
        mock_info.parent_session_id = None
        mock_registry.get_session.return_value = mock_info

        with patch(
            "src.core.orchestration.session_registry.get_session_registry",
            return_value=mock_registry,
        ):
            result = await watcher._cleanup_stale_session("parent-1")
            assert result is False

    def test_create_alert(self):
        watcher = SessionWatcher()
        alert = watcher._create_alert(
            session_id="session-1",
            level=AlertLevel.WARNING,
            title="Test Alert",
            message="Test message",
        )
        assert alert.session_id == "session-1"
        assert alert.level == AlertLevel.WARNING
        assert alert.title == "Test Alert"
        assert alert.message == "Test message"
        assert alert.alert_id is not None

    def test_emit_alert_stores_in_history(self):
        watcher = SessionWatcher()
        alert = SessionAlert(
            alert_id="test-alert",
            session_id="s1",
            level=AlertLevel.INFO,
            title="Test",
            message="Test",
            timestamp=time.time(),
        )
        watcher._emit_alert(alert)
        assert len(watcher._alert_history) == 1

    def test_emit_alert_respects_max_history(self):
        watcher = SessionWatcher()
        watcher._max_alert_history = 5
        for i in range(10):
            alert = SessionAlert(
                alert_id=f"alert-{i}",
                session_id="s1",
                level=AlertLevel.INFO,
                title="Test",
                message="Test",
                timestamp=time.time(),
            )
            watcher._emit_alert(alert)
        assert len(watcher._alert_history) == 5

    def test_emit_alert_callback_error_handled(self):
        watcher = SessionWatcher()

        def bad_callback(alert):
            raise Exception("Callback error")

        watcher.on_alert(bad_callback)
        alert = SessionAlert(
            alert_id="test",
            session_id="s1",
            level=AlertLevel.INFO,
            title="Test",
            message="Test",
            timestamp=time.time(),
        )
        # Should not raise
        watcher._emit_alert(alert)


def test_get_session_watcher():
    """Test module-level getter."""
    SessionWatcher.reset_instance()
    watcher = get_session_watcher()
    assert watcher is not None
    SessionWatcher.reset_instance()


import inspect as _inspect


def test_session_watcher_start_uses_get_running_loop():
    """Regression: start() must use get_running_loop(), not get_event_loop().

    get_event_loop() + run_until_complete(_monitor_loop()) would block the calling
    thread forever since _monitor_loop() runs until self._running is False.
    The fix uses get_running_loop() so that in a non-async context the except
    RuntimeError branch fires (no-op) rather than hanging.
    """
    from src.core.orchestration.session_watcher import SessionWatcher
    src = _inspect.getsource(SessionWatcher.start)
    assert "get_running_loop" in src, (
        "SessionWatcher.start must use asyncio.get_running_loop(), "
        "not get_event_loop() which can hang via run_until_complete"
    )
    assert "run_until_complete" not in src, (
        "SessionWatcher.start must not call run_until_complete — "
        "calling it on an infinite _monitor_loop would block forever"
    )


def test_session_registry_start_health_monitor_uses_get_running_loop():
    """Regression: SessionRegistry.start_health_monitor must use get_running_loop()."""
    from src.core.orchestration.session_registry import SessionRegistry
    src = _inspect.getsource(SessionRegistry.start_health_monitor)
    assert "get_running_loop" in src, (
        "SessionRegistry.start_health_monitor must use asyncio.get_running_loop()"
    )
    assert "run_until_complete" not in src, (
        "SessionRegistry.start_health_monitor must not call run_until_complete"
    )
