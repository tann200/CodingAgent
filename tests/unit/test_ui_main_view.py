"""Tests for orchestrator/provider event → state transitions (migrated from src.ui — LEGACY-02).

The legacy MainViewController tracked orchestrator startup and provider status
via EventBus subscriptions. This rewrite exercises the same event flow using
AgentBridge + MockEventBus so there is no dependency on src.ui.
"""

from typing import Dict, List, Callable, Any
from unittest.mock import MagicMock

# ruff: noqa: E501

from tui.tui_src.ui.mock_eventbus import get_mock_event_bus, reset_mock_event_bus
from tui.tui_src.ui.core_bridge import AgentBridge


class _MockTypedBus:
    """Minimal typed-bus stand-in: records subscribe calls and dispatches on publish."""

    def __init__(self) -> None:
        self._handlers: Dict[type, List[Callable]] = {}

    def subscribe(self, event_cls: type, handler: Callable) -> None:
        self._handlers.setdefault(event_cls, []).append(handler)

    def publish(self, event) -> None:
        for h in self._handlers.get(type(event), []):
            if hasattr(h, "handle"):
                h.handle(event)
            else:
                h(event)


def _make_bridge():
    reset_mock_event_bus()
    bus = get_mock_event_bus()
    typed_bus = _MockTypedBus()
    mock_app = MagicMock()
    # Make call_from_thread execute the callback directly (no running event loop in tests)
    mock_app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    bridge = AgentBridge.__new__(AgentBridge)
    import threading

    bridge.app = mock_app
    bridge._bus = bus
    bridge._typed_bus = typed_bus
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
    return bridge, typed_bus, mock_app


def test_startup_event_posts_orchestrator_ready_message():
    """OrchestratorStartup triggers OrchestratorReadyEvent post."""
    from src.core.messaging.event_types import OrchestratorStartup

    bridge, typed_bus, mock_app = _make_bridge()
    import time
    typed_bus.publish(OrchestratorStartup(time=time.time(), working_dir="/tmp/work"))
    mock_app.post_message.assert_called()


def test_provider_status_changed_posts_message():
    """ProviderStatusChanged fires ProviderStatusChangeEvent post on mock_app."""
    from src.core.messaging.event_types import ProviderStatusChanged

    bridge, typed_bus, mock_app = _make_bridge()
    typed_bus.publish(ProviderStatusChanged(provider="lm_studio", status="connected"))
    mock_app.post_message.assert_called()
    posted = mock_app.post_message.call_args[0][0]
    assert posted.provider == "lm_studio"
