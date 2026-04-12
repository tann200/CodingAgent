import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import warnings
import tomli
import pytest

# Enforce Python 3.11+ for test runs and fail early with a clear message.
if sys.version_info[:2] < (3, 11):
    pytest.exit(
        f"Tests require Python 3.11+ but current interpreter is {sys.version}.\n"
        "Activate the project's venv with Python 3.11+ (e.g. `python3.11 -m venv .venv && source .venv/bin/activate`)."
    )

# Add the project root to sys.path
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Suppress noisy urllib3 NotOpenSSLWarning during tests
try:
    import urllib3.exceptions as _u3e

    warnings.filterwarnings("ignore", category=_u3e.NotOpenSSLWarning)
except Exception:
    warnings.filterwarnings("ignore", message=r".*NotOpenSSLWarning.*")

# Suppress runtime warning about un-awaited coroutines from textual's call_from_thread
warnings.filterwarnings(
    "ignore", r"coroutine 'App.call_from_thread.*' was never awaited", RuntimeWarning
)
warnings.filterwarnings(
    "ignore", r"coroutine '.*run_callback' was never awaited", RuntimeWarning
)

# Suppress lancedb deprecation and vector to_df unsupported warnings
warnings.filterwarnings(
    "ignore", r".*lancedb\.pydantic\.vector\(\) is deprecated.*", DeprecationWarning
)
warnings.filterwarnings("ignore", r".*to_df\(\) is unsupported.*", Warning)

# Suppress LM Studio and Ollama adapter noisy user warnings
warnings.filterwarnings(
    "ignore", r".*LMStudioAdapter\.chat request failed.*", UserWarning
)
warnings.filterwarnings(
    "ignore", r".*get_models_from_api endpoints tried:.*", UserWarning
)

# Enforce use of project .venv when configured
try:
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    if pyproject.exists():
        with pyproject.open("rb") as f:
            cfg = tomli.load(f)
            tool_cfg = (
                cfg.get("tool", {}).get("codingagent", {})
                if isinstance(cfg.get("tool", {}), dict)
                else {}
            )
            # Some users may have [tool.codingagent] or [tool.codingagent.*]
            enforce = (
                tool_cfg.get("enforce_venv") if isinstance(tool_cfg, dict) else None
            )
            venv = tool_cfg.get("venv") if isinstance(tool_cfg, dict) else None
            if enforce and venv:
                venv_path = Path(__file__).parent.parent / venv
                # detect active venv by checking sys.prefix against the project venv path
                try:
                    if Path(sys.prefix).resolve() != venv_path.resolve():
                        raise RuntimeError(
                            f"This test run must use the project's virtualenv at {venv_path}. Activate it first: source {venv}/bin/activate"
                        )
                except Exception:
                    raise
except Exception:
    # If tomli not available or parse fails, skip enforcement to avoid blocking tests
    pass

# If Textual is installed, monkeypatch App.call_from_thread to avoid returning coroutines
try:
    import asyncio
    from textual.app import App as _TextualApp

    def _safe_call_from_thread(self, callback, *args, **kwargs):
        """Schedule callback safely on the running asyncio loop to avoid
        textual internals returning coroutines that aren't awaited in tests."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(lambda: callback(*args, **kwargs))
            else:
                # no running loop — call directly
                callback(*args, **kwargs)
        except Exception:
            try:
                callback(*args, **kwargs)
            except Exception:
                pass

    # Replace method on class
    try:
        _TextualApp.call_from_thread = _safe_call_from_thread
    except Exception:
        pass
except Exception:
    # Textual not installed or couldn't patch — ignore
    pass


# ── LEGACY-02: Inject tui/src/ui modules as src.ui.* aliases ─────────────
# tui/src/ui/core_bridge.py uses bare `from src.ui.logging import get_logger`
# which expects tui/ to be on sys.path. Since adding tui/ to sys.path hides the
# project's namespace-package src/, we instead inject tui modules as aliases.
try:
    import importlib.util as _ilu

    _TUI_UI = Path(__file__).parent.parent / "tui" / "src" / "ui"
    for _mod_stem, _alias in [
        ("logging", "src.ui.logging"),
        ("bus", "src.ui.bus"),
        ("mock_eventbus", "src.ui.mock_eventbus"),
        ("events", "src.ui.events"),
        ("settings", "src.ui.settings"),
    ]:
        _alias_full = _alias
        if _alias_full not in sys.modules:
            _p = _TUI_UI / f"{_mod_stem}.py"
            if _p.exists():
                _spec = _ilu.spec_from_file_location(_alias_full, _p)
                if _spec and _spec.loader:
                    _m = _ilu.module_from_spec(_spec)
                    sys.modules[_alias_full] = _m
                    try:
                        _spec.loader.exec_module(_m)  # type: ignore[union-attr]
                    except Exception:
                        pass  # partial init is fine; just need the name registered
except Exception:
    pass

# ── LEGACY-02: Shared bridge fixture for migrated TUI tests ────────────────


@pytest.fixture
def mock_bridge():
    """Provide (bridge, bus, mock_app) for testing AgentBridge event handling.

    Usage::

        def test_something(mock_bridge):
            bridge, bus, mock_app = mock_bridge
            bus.publish("tool.execute.start", {"tool_name": "read_file"})
            mock_app.post_message.assert_called()
    """
    from tui.src.ui.mock_eventbus import get_mock_event_bus, reset_mock_event_bus
    from tui.src.ui.core_bridge import AgentBridge

    reset_mock_event_bus()
    bus = get_mock_event_bus()
    mock_app = MagicMock()
    # Make call_from_thread execute the callback directly (no running event loop in tests)
    mock_app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    bridge = AgentBridge.__new__(AgentBridge)
    # Minimal initialisation — bypass full __init__ to avoid real bus/orchestrator.
    import threading as _threading

    bridge._app = mock_app  # type: ignore[attr-defined]
    bridge.app = mock_app  # type: ignore[assignment]
    bridge._bus = bus
    bridge._orchestrator = None
    bridge._working_dir = str(Path.cwd())
    bridge._active_role = "operational"
    bridge._agent_lock = _threading.Lock()
    bridge._agent_running = False
    bridge._history_lock = _threading.Lock()
    bridge.history = []  # public attribute (not _history)
    bridge._cancel_event = _threading.Event()
    bridge._subscriptions: list = []  # type: ignore[assignment]
    bridge.setup_subscriptions()
    return bridge, bus, mock_app


# ── NO_LM_STUDIO kill switch ──────────────────────────────────────────────────
# Set NO_LM_STUDIO=1 to guarantee that no test sends a real request to LM Studio.
# This is useful when LM Studio is running locally and you want to run the full
# test suite without accidentally triggering context-overflow spam.
#
# Effect when NO_LM_STUDIO=1:
#   1. All @pytest.mark.lmstudio tests are skipped.
#   2. The call_model LLM mock is also applied to @pytest.mark.real_llm and
#      @pytest.mark.integration tests (they no longer get real network access).
#   3. Any HTTP request whose URL contains the LM Studio base URL (default
#      http://localhost:1234) is blocked at the network layer — a hard guard
#      that catches adapter calls that bypass the call_model mock entirely.

import os as _os

_NO_LM_STUDIO = _os.getenv("NO_LM_STUDIO", "").strip() in ("1", "true", "yes")
_LM_STUDIO_BLOCK_URL = _os.getenv("LM_STUDIO_URL", "http://localhost:1234")
_RUN_INTEGRATION = _os.getenv("RUN_INTEGRATION", "").strip() in (
    "1",
    "true",
    "yes",
)


def pytest_collection_modifyitems(config, items):
    """Apply global test-suite safety skips.

    Rules:
    1) Integration/e2e tests are skipped unless RUN_INTEGRATION=1.
    2) lmstudio-marked tests are skipped when NO_LM_STUDIO=1.
    """
    skip_integration = pytest.mark.skip(
        reason="RUN_INTEGRATION!=1 — integration/e2e tests disabled"
    )
    skip_lmstudio = pytest.mark.skip(
        reason="NO_LM_STUDIO=1 — LM Studio kill switch active"
    )

    for item in items:
        nodeid = str(getattr(item, "nodeid", ""))

        # Default to fast local runs: don't execute integration/e2e unless
        # explicitly requested via RUN_INTEGRATION=1.
        if not _RUN_INTEGRATION:
            is_integration_or_e2e = (
                item.get_closest_marker("integration") is not None
                or item.get_closest_marker("e2e") is not None
                or "tests/integration/" in nodeid
                or "tests/e2e/" in nodeid
            )
            if is_integration_or_e2e:
                item.add_marker(skip_integration)

        if _NO_LM_STUDIO and item.get_closest_marker("lmstudio"):
            item.add_marker(skip_lmstudio)


# ── Global safety net: prevent real LLM network calls during tests ────────────
# Any test that accidentally calls src.core.inference.llm_manager.call_model
# without an explicit mock will get a canned response instead of hanging.
# Tests that need specific LLM responses should mock call_model themselves
# (their mock will take priority over this autouse fixture's patch via pytest
# fixture ordering or explicit context-manager patches).

_CANNED_LLM_RESPONSE = {
    "choices": [{"message": {"content": "PASS - mock response from conftest"}}]
}


@pytest.fixture(autouse=True)
def _no_real_llm_calls(request):
    """Block real LLM network calls in all unit tests.

    Integration tests (marked with @pytest.mark.integration) are exempt unless
    NO_LM_STUDIO=1 is set, in which case ALL tests get the mock.
    Tests that explicitly patch call_model themselves are unaffected because
    unittest.mock.patch stacks — the innermost patch wins.
    Tests that need the real call_model (e.g. circuit breaker tests) can opt
    out by marking with @pytest.mark.real_llm — unless NO_LM_STUDIO=1.
    """
    if not _NO_LM_STUDIO:
        if request.node.get_closest_marker("integration"):
            yield
            return
        if request.node.get_closest_marker("real_llm"):
            yield
            return

    with (
        patch(
            "src.core.inference.llm_manager.call_model",
            new=AsyncMock(return_value=_CANNED_LLM_RESPONSE),
        ),
        patch(
            "src.core.inference.llm_manager._ensure_provider_manager_initialized_sync",
            return_value=None,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _lm_studio_network_block(request):
    """Hard network-layer block for LM Studio when NO_LM_STUDIO=1.

    Intercepts requests.Session.send and raises ConnectionRefusedError if the
    prepared request targets the LM Studio base URL. This catches adapter calls
    that bypass the call_model mock (e.g. direct httpx/requests usage in tests).
    """
    if not _NO_LM_STUDIO:
        yield
        return

    import urllib.parse as _urlparse

    _block_host = _urlparse.urlparse(
        _LM_STUDIO_BLOCK_URL
    ).netloc  # e.g. "localhost:1234"

    try:
        import requests as _requests

        _orig_send = _requests.Session.send

        def _blocked_send(self, prepared_request, **kwargs):
            url = getattr(prepared_request, "url", "") or ""
            if _block_host in url:
                raise ConnectionRefusedError(
                    f"NO_LM_STUDIO=1: request to {url!r} blocked (kill switch active)"
                )
            return _orig_send(self, prepared_request, **kwargs)

        with patch.object(_requests.Session, "send", _blocked_send):
            yield
    except ImportError:
        yield
