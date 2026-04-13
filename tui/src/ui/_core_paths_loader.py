"""Helper to load src/core/paths.py from the TUI package.

TUI runs with sys.modules['src'] remapped to tui/src in dev mode, so
direct imports of src.core.* may fail.  Loading the real core.paths by
absolute file path and caching it under a fake module name avoids the
shadow while keeping behaviour identical to the canonical helpers.

This module provides a small, conservative surface: load_core_paths_module()
returns the module when available (or None), and convenience wrappers return
Path values with sensible TUI fallbacks when the core module cannot be loaded.
"""

from __future__ import annotations

import importlib.util
import sys
import os
from pathlib import Path
from types import ModuleType
from typing import Optional, Callable, cast

_MOD_NAME = "_core_paths_real"


def load_core_paths_module() -> Optional[ModuleType]:
    """Load and return the real src.core.paths module, or None on failure.

    The module is registered in sys.modules under a stable fake name so
    repeated callers reuse the same module object.
    """
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]

    try:
        path = Path(__file__).parents[3] / "src" / "core" / "paths.py"
        spec = importlib.util.spec_from_file_location(_MOD_NAME, str(path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_MOD_NAME] = mod
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        return mod
    except Exception:
        # Clean up partial registration on failure
        sys.modules.pop(_MOD_NAME, None)
        return None


# Convenience wrappers that mirror the behaviour callers expect.  These
# prefer the real core.paths helpers when the module can be loaded and
# otherwise fall back to TUI-friendly legacy locations.


def get_data_dir() -> Path:
    mod = load_core_paths_module()
    if mod is not None:
        fn = getattr(mod, "get_data_dir", None)
        if callable(fn):
            return Path(fn())

    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "CodingAgent"
    return Path.home() / ".coding_agent"


def get_sessions_dir() -> Path:
    mod = load_core_paths_module()
    if mod is not None:
        fn = getattr(mod, "get_sessions_dir", None)
        if callable(fn):
            return Path(fn())

    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "CodingAgent" / "sessions"
    return Path.home() / ".coding_agent" / "sessions"


def get_config_dir() -> Path:
    # TUI config historically fell back to ~/.agent_tui when core was
    # unavailable; preserve that behaviour.
    mod = load_core_paths_module()
    if mod is not None:
        fn = getattr(mod, "get_config_dir", None)
        if callable(fn):
            return Path(fn())

    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "CodingAgent"
    return Path.home() / ".agent_tui"


def get_log_dir() -> Path:
    mod = load_core_paths_module()
    if mod is not None:
        fn = getattr(mod, "get_data_dir", None)
        if callable(fn):
            return Path(fn()) / "logs"

    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "CodingAgent" / "logs"
    return Path.home() / ".agent_tui" / "logs"
