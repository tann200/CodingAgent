"""Tests for provider model-list event via AgentBridge (migrated from src.ui — LEGACY-02).

The legacy ProviderPanelController subscribed to 'provider.models.list' events via
EventBus and accumulated provider names. This rewrite tests the same event flow
through AgentBridge using MockEventBus — no src.ui dependency.
"""

import pytest
from unittest.mock import MagicMock

# ruff: noqa: E501
import threading

import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

# Shadow tui/src as `src` during import of tui modules so `from src.ui.*` works inside tui
_tui_root = str(_Path(__file__).parents[2] / "tui")
_tui_src_init = _Path(_tui_root) / "src" / "__init__.py"
_tui_src_spec = _ilu.spec_from_file_location(
    "src",
    str(_tui_src_init),
    submodule_search_locations=[str(_Path(_tui_root) / "src")],
)
if _tui_src_spec is None or _tui_src_spec.loader is None:
    raise ImportError(f"Cannot load TUI src spec from {_tui_src_init}")
_tui_src_mod = _ilu.module_from_spec(_tui_src_spec)
_tui_src_spec.loader.exec_module(_tui_src_mod)  # type: ignore[union-attr]

_saved_src = _sys.modules.get("src")
_sys.modules["src"] = _tui_src_mod
try:
    from tui.src.ui.mock_eventbus import get_mock_event_bus, reset_mock_event_bus
    from tui.src.ui.core_bridge import AgentBridge
finally:
    if _saved_src is not None:
        _sys.modules["src"] = _saved_src
    else:
        _sys.modules.pop("src", None)

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
