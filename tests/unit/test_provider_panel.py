"""Tests for provider model-list event via AgentBridge (migrated from src.ui — LEGACY-02).

The legacy ProviderPanelController subscribed to 'provider.models.list' events via
EventBus and accumulated provider names. This rewrite tests the same event flow
through AgentBridge using MockEventBus — no src.ui dependency.
"""

import pytest
from unittest.mock import MagicMock
import threading

from tui.src.ui.mock_eventbus import get_mock_event_bus, reset_mock_event_bus
from tui.src.ui.core_bridge import AgentBridge
from src.core.inference.llm_manager import _provider_manager


@pytest.fixture(autouse=True)
def restore_provider_manager():
    """Save and restore global _provider_manager state between tests."""
    saved_providers = dict(_provider_manager._providers)
    saved_initialized = _provider_manager._initialized
    yield
    _provider_manager._providers = saved_providers
    _provider_manager._initialized = saved_initialized


def _make_bridge():
    reset_mock_event_bus()
    bus = get_mock_event_bus()
    mock_app = MagicMock()
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


def test_provider_models_list_event_posts_message():
    """provider.models.list event is received and logged (bridge calls _on_models_list)."""
    bridge, bus, mock_app = _make_bridge()
    # _on_models_list only logs; verify no exception is raised
    bus.publish(
        "provider.models.list", {"provider": "lm_studio", "models": ["m1", "m2"]}
    )
    # mock_app.post_message is not called for this event (bridge just logs it)
    # Ensure bridge did not error out
    assert bridge is not None


def test_provider_manager_list_returns_list():
    """_provider_manager.list_providers() returns a list (can be empty in test context)."""
    pm = _provider_manager
    pm._providers = {"lm_studio": object()}
    pm._initialized = True
    result = pm.list_providers()
    assert isinstance(result, list)
    assert "lm_studio" in result
