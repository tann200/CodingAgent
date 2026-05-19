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
        from src.tools.tools_config import get_context_dir_name

        ctx_name = get_context_dir_name()
    except Exception:
        ctx_name = ".codingAgent"

    cwd = Path.cwd()
    candidate = cwd / ctx_name
    return [candidate / "config.json", candidate / "config.local.json"]


def load_merged_config(working_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load and deep-merge all configuration layers.

    If ``working_dir`` is provided, workspace layer files are resolved relative
    to that directory (``<working_dir>/.agent/config.json`` and
    ``config.local.json``). This mirrors test expectations which call
    ``load_merged_config(working_dir=...)``.
    """
    result: Dict[str, Any] = {}

    # Always include bundled defaults and user config; workspace layer depends on working_dir
    paths = [_BUNDLED_DEFAULTS, _USER_CONFIG]
    if working_dir is None:
        workspace_paths = _get_workspace_config_paths()
    else:
        # Resolve the workspace agent directory using the central helper when
        # available. This ensures consistent semantics across code and tests.
        try:
            from src.tools.tools_config import agent_context_path

            wd = Path(working_dir)
            agent_dir = agent_context_path(wd)
            workspace_paths = [
                agent_dir / "config.json",
                agent_dir / "config.local.json",
            ]
        except Exception:
            # Fallback to prior heuristic when tools_config is unavailable
            try:
                from src.tools.tools_config import get_context_dir_name

                ctx_name = get_context_dir_name()
            except Exception:
                ctx_name = ".codingAgent"
            wd = Path(working_dir)
            candidate = wd / ctx_name
            if candidate.exists():
                workspace_paths = [
                    candidate / "config.json",
                    candidate / "config.local.json",
                ]
            else:
                workspace_paths = [
                    candidate / "config.json",
                    candidate / "config.local.json",
                ]
    paths.extend(workspace_paths)
    skipped = []

    for path in paths:
        if path.exists():
            data = _load_config_file(path)
            # providers.json is a list at the top-level; treat lists as the `providers`
            # top-level key to avoid passing a list into _deep_merge (which expects dicts).
            if isinstance(data, dict):
                result = _deep_merge(result, data)
            elif isinstance(data, list):
                # Prefer later layers to override earlier ones; assign directly.
                result["providers"] = data
            else:
                # Unknown payload type — skip
                continue
        else:
            skipped.append(str(path))

    if skipped:
        logger.debug(f"Skipped missing config paths: {skipped}")

    # Apply hot-reload update if enabled
    if get_config_reloader is not None:
        try:
            # get_config_reloader takes the current merged config as its
            # initial state and returns an object with changed()/load().
            reloader = get_config_reloader(result)
            try:
                if reloader.changed():
                    logger.debug("Hot-reload detected config change, applying")
                    result = reloader.load()
            except Exception as exc:
                logger.debug("config_loader: hot-reload interface error: %s", exc)
        except Exception as e:
            logger.debug(f"Hot-reload not available: {e}")

    return result


def get_agent_config_path() -> Path:
    """Return the active agent context configuration directory."""
    try:
        return get_agent_context_dir()
    except Exception:
        return Path.cwd() / ".codingAgent"


# Global config cache
_cached_config: Optional[Dict[str, Any]] = None
_last_load_time: float = 0.0
_config_lock: threading.Lock = threading.Lock()


def get_global_config() -> Dict[str, Any]:
    """Get the global merged configuration with thread-safe caching."""
    global _cached_config, _last_load_time
    with _config_lock:
        if _cached_config is None or time.time() - _last_load_time > 300:  # 5 min cache
            _cached_config = load_merged_config()
            _last_load_time = time.time()
        return _cached_config


def get(key: str, default: Any = None, working_dir: Optional[Path] = None) -> Any:
    """Convenience shortcut for fetching a single config value.

    Example: get("max_turns", default=50, working_dir=tmpdir)
    """
    cfg = load_merged_config(working_dir=working_dir)
    if not isinstance(cfg, dict):
        return default
    return cfg.get(key, default)


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

    # Use the central merged config loader which already implements the
    # configured context-dir name and legacy fallbacks. Forward the
    # working_dir so callers (tests) get workspace-local overrides.
    merged = load_merged_config(working_dir=working_dir)
    mcp_cfg = merged.get("mcp", {}) if isinstance(merged, dict) else {}

    # Deep-merge mcp_cfg onto defaults so callers get consistent keys.
    return _deep_merge(defaults, mcp_cfg)


def get_mcp_servers(working_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return the list of configured MCP servers (may be empty)."""
    cfg = get_mcp_config(working_dir=working_dir)
    servers = cfg.get("servers") if isinstance(cfg, dict) else None
    if not isinstance(servers, list):
        return []

    # Add Context7 test server if no servers configured
    if not servers:
        servers = [
            {
                "name": "context7",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp"],
            }
        ]

    return servers


# Keys for per-role model overrides in providers.json / config
_ROLE_MODEL_KEYS = {
    "planning_model": "strategic",
    "execution_model": "operational",
}


def get_model_for_role(role: str) -> Optional[str]:
    """Return an optional model id for a given role (e.g. 'strategic').

    Looks into the bundled providers.json (and later merged config via
    load_merged_config) for a provider entry with an override for the role.
    Returns the first matching active provider's model override, or None.
    """
    try:
        cfg = load_merged_config()
        # providers may be stored under "providers" (we normalize lists there)
        providers = cfg.get("providers") if isinstance(cfg, dict) else None
        if not providers:
            return None
        # providers is expected to be a list of dicts
        for p in providers:
            if not isinstance(p, dict):
                continue
            if not p.get("active"):
                continue
            # Check per-role keys
            for key, role_name in _ROLE_MODEL_KEYS.items():
                if role_name == role and key in p:
                    return p.get(key)
    except Exception:
        return None
    return None



# ---------------------------------------------------------------------------
# Agent loop constants — all hardcoded defaults overridable via config keys
# ---------------------------------------------------------------------------

#: Maximum number of planning rounds before the planner gives up.
#: Config key: ``max_rounds_planning``
MAX_ROUNDS_PLANNING: int = 15

#: Seconds before an existing plan is considered stale and replanned.
#: Config key: ``plan_resume_ttl_seconds``
PLAN_RESUME_TTL_SECONDS: int = 1800

#: Default per-task debug attempt ceiling (also readable from state["max_debug_attempts"]).
#: Config key: ``max_debug_attempts``
MAX_DEBUG_ATTEMPTS: int = 3

#: Session-wide ceiling on total debug LLM calls (across all tasks/steps).
#: Config key: ``total_debug_ceiling``
TOTAL_DEBUG_CEILING: int = 9

#: Maximum replan attempts per execution path.
#: Config key: ``max_replan_attempts``
MAX_REPLAN_ATTEMPTS: int = 5


def get_agent_loop_constant(key: str, default: int, working_dir: Optional[Path] = None) -> int:
    """Read an integer agent-loop constant from merged config, falling back to *default*.

    Useful for nodes that want config-driven limits without hard-coding values.
    """
    try:
        cfg = load_merged_config(working_dir=working_dir)
        val = cfg.get(key)
        if val is not None:
            return int(val)
    except Exception:
        pass
    return default


def get_small_model(working_dir: Optional[Path] = None) -> Optional[str]:
    """Return a configured 'small_model' string.

    Preference order:
      1. workspace .agent/config.json small_model
      2. active provider small_model entry in providers.json
      3. None
    """
    # 1. workspace override — check canonical .codingAgent config
    if working_dir is not None:
        try:
            from src.tools.tools_config import agent_context_path
            agent_dir = agent_context_path(Path(working_dir))
        except Exception:
            agent_dir = Path(working_dir) / ".codingAgent"
        cfg: Dict[str, Any] = {}
        for p in (agent_dir / "config.json", agent_dir / "config.local.json"):
            if p.exists():
                cfg = _deep_merge(cfg, _load_config_file(p))
        if isinstance(cfg, dict) and "small_model" in cfg:
            return cfg.get("small_model")

    # 2. active provider
    try:
        merged = load_merged_config(working_dir=working_dir)
        providers = merged.get("providers") if isinstance(merged, dict) else None
        if isinstance(providers, list):
            for p in providers:
                if isinstance(p, dict) and p.get("active") and "small_model" in p:
                    return p.get("small_model")
    except Exception:
        return None
    return None


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

        watch_dir = Path(self._working_dir) / ".codingAgent"
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
