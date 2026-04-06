import sys
from pathlib import Path
from unittest.mock import MagicMock
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
