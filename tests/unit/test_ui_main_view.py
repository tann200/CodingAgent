"""Tests for orchestrator/provider event → state transitions (migrated from src.ui — LEGACY-02).

The legacy MainViewController tracked orchestrator startup and provider status
via EventBus subscriptions. This rewrite exercises the same event flow using
AgentBridge + MockEventBus so there is no dependency on src.ui.
"""

from unittest.mock import MagicMock
from tui.src.ui.mock_eventbus import get_mock_event_bus, reset_mock_event_bus
from tui.src.ui.core_bridge import AgentBridge


def _make_bridge():
    reset_mock_event_bus()
    bus = get_mock_event_bus()
    mock_app = MagicMock()
    # Make call_from_thread execute the callback directly (no running event loop in tests)
    mock_app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    bridge = AgentBridge.__new__(AgentBridge)
    import threading

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


def test_startup_event_posts_orchestrator_ready_message():
    """orchestrator.startup (remapped via _EVENT_MAP) triggers OrchestratorReadyEvent post."""
    bridge, bus, mock_app = _make_bridge()
    # _EVENT_MAP maps "orchestrator.startup" → "system.startup"
    bus.publish("system.startup", {"working_dir": "/tmp/work"})
    mock_app.post_message.assert_called()


def test_provider_status_changed_posts_message():
    """provider.status.changed fires ProviderStatusChangeEvent post on mock_app."""
    bridge, bus, mock_app = _make_bridge()
    bus.publish(
        "provider.status.changed", {"provider": "lm_studio", "status": "connected"}
    )
    mock_app.post_message.assert_called()
    posted = mock_app.post_message.call_args[0][0]
    assert posted.provider == "lm_studio"
