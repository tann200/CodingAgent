"""Hierarchical configuration loader.

Merges configuration from four layers (later layers override earlier ones):

1. ``src/config/providers.json``         — bundled defaults (committed to repo)
2. ``~/.config/codingagent/config.json``   — user-level overrides
3. ``<cwd>/.agent/config.json``           — workspace config (committable)
4. ``<cwd>/.agent/config.local.json``     — local overrides (add to .gitignore)

Dictionaries are deep-merged; scalar values and lists are replaced by the
later layer.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
import importlib.util
import threading
from typing import Any, Dict, List, Optional, Callable

# Lazy import to avoid circular dependency with hot reload
try:
    from src.core.config_hot_reload import get_config_reloader
except Exception:
    get_config_reloader = None

from src.core.paths import get_user_config_path, get_agent_context_dir

logger = logging.getLogger(__name__)

# Path to bundled defaults (relative to source root)
_REPO_ROOT = Path(__file__).parents[2]
_BUNDLED_DEFAULTS = _REPO_ROOT / "src" / "config" / "providers.json"
_USER_CONFIG = get_user_config_path()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict that is *base* deep-merged with *override*.

    Dicts at the same key are recursively merged; all other types are
    replaced by *override*'s value.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_config_file(path: Path) -> Dict[str, Any]:
    """Load a single YAML/JSON config file."""
    if not path.exists():
        return {}
    try:
        if path.suffix in {".yaml", ".yml"}:
            import yaml

            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        elif path.suffix == ".json":
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load config {path}: {e}")
    return {}


def _get_workspace_config_paths() -> List[Path]:
    """Get workspace config paths (cwd-based)."""
    try:
        cwd = Path.cwd()
        agent_dir = cwd / ".agent"
        return [
            agent_dir / "config.json",
            agent_dir / "config.local.json",
        ]
    except Exception:
        return []


def load_merged_config() -> Dict[str, Any]:
    """Load and deep-merge all configuration layers."""
    result: Dict[str, Any] = {}
    paths = [_BUNDLED_DEFAULTS, _USER_CONFIG]
    paths.extend(_get_workspace_config_paths())
    skipped = []

    for path in paths:
        if path.exists():
            data = _load_config_file(path)
            result = _deep_merge(result, data)
        else:
            skipped.append(str(path))

    if skipped:
        logger.debug(f"Skipped missing config paths: {skipped}")

    # Apply hot-reload update if enabled
    if get_config_reloader is not None:
        try:
            reloader = get_config_reloader(result)
            if reloader.changed():
                logger.debug("Hot-reload detected config change, applying")
                result = reloader.load()
        except Exception as e:
            logger.debug(f"Hot-reload not available: {e}")

    return result


def get_agent_config_path() -> Path:
    """Return the active agent context configuration directory."""
    try:
        return get_agent_context_dir()
    except Exception:
        return Path.cwd() / ".agent-context"


# Global config cache
_cached_config: Optional[Dict[str, Any]] = None
_last_load_time: float = 0.0


def get_global_config() -> Dict[str, Any]:
    """Get the global merged configuration with simple caching."""
    global _cached_config, _last_load_time
    if _cached_config is None or time.time() - _last_load_time > 300:  # 5 min cache
        _cached_config = load_merged_config()
        _last_load_time = time.time()
    return _cached_config


# ---------------------------------------------------------------------------
# MCP config helpers (tests and MCP manager expect these)
# ---------------------------------------------------------------------------


def get_mcp_config(working_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return the resolved MCP configuration dict.

    The function returns a dict containing at least the keys:
      - servers: list
      - timeout_seconds: int
      - auto_register_tools: bool

    When "working_dir" is provided, workspace overrides are loaded from
    ``<working_dir>/.agent/config.json`` and ``config.local.json``. When
    omitted, the normal merged config is used.
    """
    defaults = {"servers": [], "timeout_seconds": 30, "auto_register_tools": True}

    mcp_cfg: Dict[str, Any] = {}
    if working_dir is None:
        merged = load_merged_config()
        mcp_cfg = merged.get("mcp", {}) if isinstance(merged, dict) else {}
    else:
        agent_dir = Path(working_dir) / ".agent"
        cfg: Dict[str, Any] = {}
        for p in (agent_dir / "config.json", agent_dir / "config.local.json"):
            if p.exists():
                cfg = _deep_merge(cfg, _load_config_file(p))
        mcp_cfg = cfg.get("mcp", {}) if isinstance(cfg, dict) else {}

    # Deep-merge mcp_cfg onto defaults so callers get consistent keys.
    return _deep_merge(defaults, mcp_cfg)


def get_mcp_servers(working_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return the list of configured MCP servers (may be empty)."""
    cfg = get_mcp_config(working_dir=working_dir)
    servers = cfg.get("servers") if isinstance(cfg, dict) else None
    if not isinstance(servers, list):
        return []
    return servers


# ---------------------------------------------------------------------------
# ConfigWatcher — simple optional watcher wrapper around ``watchfiles``
# (keeps tests hermetic when watchfiles is absent)
# ---------------------------------------------------------------------------


class ConfigWatcher:
    """Watch configuration files and invoke callbacks on change.

    The implementation is intentionally small and resilient — it will be a
    no-op when the optional ``watchfiles`` dependency is not available.
    """

    def __init__(
        self,
        *,
        working_dir: Optional[Path] = None,
        event_bus: Optional[Any] = None,
        reload_callbacks: Optional[List[Callable[[set], None]]] = None,
    ) -> None:
        self._working_dir: Path = (
            Path(working_dir) if working_dir is not None else Path.cwd()
        )
        self._event_bus = event_bus
        self._callbacks: List[Callable[[set], None]] = (
            list(reload_callbacks) if reload_callbacks else []
        )
        self._stop_flag: bool = False
        self._thread: Optional[threading.Thread] = None
        # Pre-compute availability to let tests override easily
        self._available: bool = self._check_watchfiles()

    def add_callback(self, cb: Callable[[set], None]) -> None:
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    @staticmethod
    def _check_watchfiles() -> bool:
        """Return True when the optional ``watchfiles`` module appears importable."""
        try:
            return importlib.util.find_spec("watchfiles") is not None
        except Exception:
            return False

    def _on_change(self, changed_paths: set) -> None:
        # Invoke all callbacks (exceptions swallowed)
        for cb in list(self._callbacks):
            try:
                cb(changed_paths)
            except Exception:
                pass

        # Publish event if an event bus is provided (exceptions swallowed)
        if self._event_bus is not None:
            try:
                payload = {"changed_paths": list(changed_paths)}
                self._event_bus.publish("config.reloaded", payload)
            except Exception:
                pass

    def start(self) -> bool:
        """Start the watch loop in a daemon thread. Returns True when started.

        If the optional watchfiles module is not available, returns False.
        Subsequent calls are idempotent while the thread is alive.
        """
        if not self._available:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        # Clear/ensure stop flag
        self._stop_flag = False
        t = threading.Thread(
            target=self._watch_loop, daemon=True, name="config-watcher"
        )
        self._thread = t
        t.start()
        return True

    def stop(self) -> None:
        self._stop_flag = True

    def _watch_loop(self) -> None:
        """Watch the workspace .agent directory using watchfiles.watch.

        The function tolerates different event payload shapes and will call
        ``_on_change`` with a set of string file paths.  If ``_stop_flag`` is
        set before handling a change, the change is ignored and the loop
        exits.
        """
        try:
            import watchfiles
        except Exception:
            return

        watch_dir = Path(self._working_dir) / ".agent"
        # If the directory doesn't exist, still call watchfiles.watch so that
        # tests that inject a fake module (or real watchfiles) can still drive
        # the loop.
        try:
            for changes in watchfiles.watch(str(watch_dir), stop_event=None):
                # Exit early if requested
                if self._stop_flag:
                    break

                changed: set = set()
                try:
                    for ch in changes:
                        if isinstance(ch, tuple):
                            if len(ch) >= 2:
                                changed.add(str(ch[1]))
                            else:
                                changed.add(str(ch[0]))
                        else:
                            changed.add(str(ch))
                except Exception:
                    # Ignore malformed change entries
                    continue

                # If stop requested after receiving the change, exit without
                # invoking callbacks (tests expect this behaviour).
                if self._stop_flag:
                    break

                if changed:
                    self._on_change(changed)
        finally:
            # Clear thread reference on exit
            try:
                self._thread = None
            except Exception:
                pass
