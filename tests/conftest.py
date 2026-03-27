import sys
from pathlib import Path
import warnings
import tomli
import pytest

# Enforce Python 3.11+ for test runs and fail early with a clear message.
if sys.version_info[:2] < (3, 11):
    pytest.exit(
        f"Tests require Python 3.11+ but current interpreter is {sys.version}.\n"
        "Activate the project's venv with Python 3.11+ (e.g. `python3.11 -m venv .venv && source .venv/bin/activate`)."
    )

# Add the src directory to the sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Suppress noisy urllib3 NotOpenSSLWarning during tests
try:
    import urllib3.exceptions as _u3e
    warnings.filterwarnings('ignore', category=_u3e.NotOpenSSLWarning)
except Exception:
    warnings.filterwarnings('ignore', message=r'.*NotOpenSSLWarning.*')

# Suppress runtime warning about un-awaited coroutines from textual's call_from_thread
warnings.filterwarnings('ignore', r"coroutine 'App.call_from_thread.*' was never awaited", RuntimeWarning)
warnings.filterwarnings('ignore', r"coroutine '.*run_callback' was never awaited", RuntimeWarning)

# Suppress lancedb deprecation and vector to_df unsupported warnings
warnings.filterwarnings('ignore', r'.*lancedb\.pydantic\.vector\(\) is deprecated.*', DeprecationWarning)
warnings.filterwarnings('ignore', r'.*to_df\(\) is unsupported.*', Warning)

# Suppress LM Studio and Ollama adapter noisy user warnings
warnings.filterwarnings('ignore', r'.*LMStudioAdapter\.chat request failed.*', UserWarning)
warnings.filterwarnings('ignore', r'.*get_models_from_api endpoints tried:.*', UserWarning)

# Enforce use of project .venv when configured
try:
    pyproject = Path(__file__).parent.parent / 'pyproject.toml'
    if pyproject.exists():
        with pyproject.open('rb') as f:
            cfg = tomli.load(f)
            tool_cfg = cfg.get('tool', {}).get('codingagent', {}) if isinstance(cfg.get('tool', {}), dict) else {}
            # Some users may have [tool.codingagent] or [tool.codingagent.*]
            enforce = tool_cfg.get('enforce_venv') if isinstance(tool_cfg, dict) else None
            venv = tool_cfg.get('venv') if isinstance(tool_cfg, dict) else None
            if enforce and venv:
                venv_path = Path(__file__).parent.parent / venv
                # detect active venv by checking sys.prefix against the project venv path
                try:
                    if Path(sys.prefix).resolve() != venv_path.resolve():
                        raise RuntimeError(f"This test run must use the project's virtualenv at {venv_path}. Activate it first: source {venv}/bin/activate")
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
