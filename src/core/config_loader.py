"""Hierarchical configuration loader.

Merges configuration from four layers (later layers override earlier ones):

1. ``src/config/providers.json``       — bundled defaults (committed to repo)
2. ``~/.config/codingagent/config.json`` — user-level overrides
3. ``<cwd>/.agent/config.json``         — workspace config (committable)
4. ``<cwd>/.agent/config.local.json``   — local overrides (add to .gitignore)

Dicts are deep-merged; scalar values and lists are replaced by the later layer.

Usage::

    from src.core.config_loader import load_merged_config

    cfg = load_merged_config()
    providers = cfg.get("providers", [])
    max_turns = cfg.get("max_turns", 50)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.paths import get_user_config_path

logger = logging.getLogger(__name__)

# Path to the bundled defaults (relative to this file's package root)
_REPO_ROOT = Path(__file__).parents[2]
_BUNDLED_DEFAULTS = _REPO_ROOT / "src" / "config" / "providers.json"

# User-level config
_USER_CONFIG = get_user_config_path()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict that is *base* deep-merged with *override*.

    Dicts at the same key are recursively merged; all other types are replaced
    by *override*'s value.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_json(path: Path) -> Dict[str, Any]:
    """Load a JSON file and return its contents as a dict.

    Returns an empty dict silently on any error so callers never need to guard.
    """
    try:
        if not path.is_file():
            return {}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        # providers.json at the root is a JSON array, not an object.
        # Normalise it so callers always get a dict.
        if isinstance(data, list):
            return {"providers": data}
        if isinstance(data, dict):
            return data
        return {}
    except Exception as exc:
        logger.debug("config_loader: failed to load %s: %s", path, exc)
        return {}


def load_merged_config(working_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return the merged configuration dict.

    Parameters
    ----------
    working_dir:
        Directory used to locate ``.agent/config.json`` and
        ``.agent/config.local.json``.  Defaults to ``Path.cwd()``.

    Returns
    -------
    dict
        Deep-merged configuration.  Always returns a dict (never raises).
    """
    if working_dir is None:
        working_dir = Path(os.getcwd())

    # AUTO-01: Built-in defaults so callers get sane values without needing
    # any config file present on disk.
    merged: Dict[str, Any] = {
        "autonomous_mode": False,
        "max_turns": 50,
        # PREV-1: When True, file-write tools show a diff and wait for
        # Accept/Reject before applying the change.  Default False (auto-accept).
        "preview_confirmation": False,
        # TASK-07: token-based compaction threshold (estimated tokens)
        "compact_token_threshold": 6000,
        # SM-1: Optional lightweight model used for background tasks
        # (session title generation, context compaction/distillation, subagent
        # routing).  When None, background tasks use the same model as the
        # active provider default.  May be overridden per-provider via
        # providers.json ``small_model`` field.
        "small_model": None,
        # S3-B: MCP server configuration section.
        # Each entry in ``mcp.servers`` describes an outbound MCP server the
        # agent will connect to on startup.
        #
        # Example workspace .agent/config.json:
        #
        #   {
        #     "mcp": {
        #       "servers": [
        #         {
        #           "name": "filesystem",
        #           "cmd": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
        #           "auto_register_tools": true
        #         }
        #       ]
        #     }
        #   }
        "mcp": {
            "servers": [],  # list of McpServerConfig dicts (see below)
            "timeout_seconds": 30,  # per-call timeout for MCP RPC calls
            "auto_register_tools": True,  # register tools from MCP into ToolRegistry
        },
        # RS-1: Remote skill discovery.
        # List of base URLs that expose a skill index at <url>/index.json.
        # Each URL's index.json should be a list of objects with at minimum:
        #   {"name": "skill_name", "file": "skill_name.md"}
        # Individual skill files are fetched from <url>/<file> and cached in
        # the CodingAgent cache directory (see src.core.paths.get_skills_cache_dir()).
        #
        # Example .agent/config.json:
        #   {
        #     "skills": {
        #       "urls": ["https://raw.githubusercontent.com/myorg/skills/main/"]
        #     }
        #   }
        "skills": {
            "urls": [],  # list of remote skill index base URLs
            "cache_ttl_seconds": 3600,  # how long to keep cached remote skills
        },
    }

    layers = [
        _BUNDLED_DEFAULTS,
        _USER_CONFIG,
        working_dir / ".agent" / "config.json",
        working_dir / ".agent" / "config.local.json",
    ]

    for path in layers:
        data = _load_json(path)
        if data:
            merged = _deep_merge(merged, data)
            logger.debug("config_loader: loaded layer %s", path)

    return merged


def get(key: str, default: Any = None, working_dir: Optional[Path] = None) -> Any:
    """Convenience accessor: load merged config and return *key*.

    Parameters
    ----------
    key:
        Top-level config key.
    default:
        Returned when *key* is absent.
    working_dir:
        Forwarded to ``load_merged_config()``.
    """
    return load_merged_config(working_dir).get(key, default)


# AUTO-02: Default per-role autonomy/permission settings.
# The ``roles`` section of the merged config may override any of these.
_DEFAULT_ROLE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "lead_architect": {
        "autonomous": False,
        "max_turns": 50,
        "permission_mode": "prompt",
    },
    "full_stack_engineer": {
        "autonomous": True,
        "max_turns": 100,
        "permission_mode": "workspace_write",
    },
    "qa_lead": {
        "autonomous": True,
        "max_turns": 30,
        "permission_mode": "read_only",
    },
}


class ConfigWatcher:
    """S6-C: File-system watcher for config hot-reload.

    Watches ``providers.json`` and ``.agent/config.json`` for changes.
    When a change is detected, calls registered reload callbacks and publishes
    a ``config.reloaded`` event on the EventBus.

    Requires the optional ``watchfiles`` package (``pip install watchfiles``).
    When unavailable, the watcher silently becomes a no-op and the config is
    only loaded at startup (the existing behaviour is preserved).

    Usage::

        watcher = ConfigWatcher(working_dir=Path("."), event_bus=bus)
        watcher.start()   # starts a daemon background thread
        # … application runs …
        watcher.stop()
    """

    def __init__(
        self,
        working_dir: Optional[Path] = None,
        event_bus: Any = None,
        reload_callbacks: Optional[list] = None,
    ) -> None:
        self._working_dir = working_dir or Path(os.getcwd())
        self._event_bus = event_bus
        self._callbacks: list = list(reload_callbacks or [])
        self._thread: Any = None
        self._stop_flag = False
        self._available = self._check_watchfiles()

    @staticmethod
    def _check_watchfiles() -> bool:
        try:
            import watchfiles  # type: ignore[import]  # noqa: F401

            return True
        except ImportError:
            return False

    def add_callback(self, fn) -> None:
        """Register a callable invoked (with the changed paths set) on config change."""
        self._callbacks.append(fn)

    def start(self) -> bool:
        """Start watching in a daemon background thread.

        Returns True if watchfiles is available and the thread was started;
        False if watchfiles is absent (graceful no-op).
        """
        if not self._available:
            logger.debug(
                "config_loader: watchfiles not installed — hot-reload disabled"
            )
            return False
        if self._thread and self._thread.is_alive():
            return True

        import threading

        self._stop_flag = False
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="config-watcher",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "config_loader: ConfigWatcher started (watching %s)", self._working_dir
        )
        return True

    def stop(self) -> None:
        """Signal the background thread to exit on the next watch iteration."""
        self._stop_flag = True

    def _watch_loop(self) -> None:
        """Background thread: use watchfiles.watch() to detect file changes."""
        try:
            import watchfiles  # type: ignore[import]

            watch_paths = [
                str(_BUNDLED_DEFAULTS),
                str(self._working_dir / ".agent" / "config.json"),
                str(self._working_dir / ".agent" / "config.local.json"),
            ]
            # Only watch paths that actually exist
            existing = [p for p in watch_paths if Path(p).exists()]
            if not existing:
                logger.debug("config_loader: no config files to watch yet")
                return

            for changes in watchfiles.watch(*existing, stop_event=None):
                if self._stop_flag:
                    break
                changed_paths = {str(c[1]) for c in changes}
                logger.info("config_loader: config changed: %s", changed_paths)
                self._on_change(changed_paths)

        except Exception as exc:
            logger.debug("config_loader: watch loop exited: %s", exc)

    def _on_change(self, changed_paths: set) -> None:
        """Invoke callbacks and publish event on config change."""
        for cb in self._callbacks:
            try:
                cb(changed_paths)
            except Exception as exc:
                logger.warning("config_loader: reload callback error: %s", exc)

        try:
            if self._event_bus:
                self._event_bus.publish(
                    "config.reloaded",
                    {"changed_paths": list(changed_paths)},
                )
        except Exception as exc:
            logger.debug("config_loader: failed to publish config.reloaded: %s", exc)


def get_role_config(role: str, working_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return the autonomy/permission configuration for *role*.

    Merges the built-in defaults from ``_DEFAULT_ROLE_CONFIGS`` with any
    ``roles.<role>`` section found in the merged config file layers.

    Parameters
    ----------
    role:
        One of the TUI role names: ``"lead_architect"``,
        ``"full_stack_engineer"``, or ``"qa_lead"``.  Unknown roles fall back
        to the ``lead_architect`` defaults.
    working_dir:
        Forwarded to ``load_merged_config()``.

    Returns
    -------
    dict
        Keys: ``autonomous`` (bool), ``max_turns`` (int),
        ``permission_mode`` (str).
    """
    cfg = load_merged_config(working_dir)
    role_overrides: Dict[str, Any] = cfg.get("roles", {}).get(role, {})
    base = dict(
        _DEFAULT_ROLE_CONFIGS.get(role, _DEFAULT_ROLE_CONFIGS["lead_architect"])
    )
    base.update(role_overrides)
    return base


def get_mcp_config(working_dir: Optional[Path] = None) -> Dict[str, Any]:
    """S3-B: Return the merged MCP configuration section.

    Returns the ``mcp`` dict from the merged config, which includes:
    - ``servers``: list of server definition dicts (name, cmd, auto_register_tools)
    - ``timeout_seconds``: per-call RPC timeout
    - ``auto_register_tools``: whether to register server tools into the registry

    Parameters
    ----------
    working_dir:
        Forwarded to ``load_merged_config()``.

    Returns
    -------
    dict
        MCP configuration section.  Always returns a dict (never raises).
    """
    cfg = load_merged_config(working_dir)
    return cfg.get(
        "mcp", {"servers": [], "timeout_seconds": 30, "auto_register_tools": True}
    )


def get_mcp_servers(working_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """S3-B: Return the list of configured MCP server definitions.

    Each entry is a dict with at minimum:
    - ``name`` (str): unique server name
    - ``cmd`` (list[str]): command to launch the server subprocess
    - ``auto_register_tools`` (bool, optional): default True

    Parameters
    ----------
    working_dir:
        Forwarded to ``load_merged_config()``.
    """
    return get_mcp_config(working_dir).get("servers", [])


# SES-W3: Per-model-per-role config
# ---------------------------------------------------------------------------

_ROLE_MODEL_KEYS = {
    "planning": "planning_model",
    "strategic": "planning_model",
    "execution": "execution_model",
    "operational": "execution_model",
    "analyst": "analyst_model",
    "reviewer": "reviewer_model",
    "debugger": "debugger_model",
}


def get_model_for_role(
    role: str,
    working_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return the model name configured for *role* in the active provider.

    Reads the ``planning_model``, ``execution_model``, etc. keys from the
    active provider entry in ``providers.json``.  Returns ``None`` when no
    per-role override is configured, signalling that the provider default
    should be used.

    Args:
        role: The agent role name, e.g. ``"strategic"``, ``"operational"``.
        working_dir: Optional working directory for config lookup.

    Returns:
        Model name string, or ``None`` if no per-role override exists.
    """
    config_key = _ROLE_MODEL_KEYS.get(role.lower())
    if not config_key:
        return None
    try:
        # Read providers array from providers.json
        bundled = _BUNDLED_DEFAULTS
        raw = bundled.read_text(encoding="utf-8") if bundled.is_file() else "[]"
        # LOGIC-3: Parse once; avoid calling json.loads(raw) twice on the same string.
        parsed = json.loads(raw)
        providers_list = parsed if isinstance(parsed, list) else []
        # Find the active provider
        active = next((p for p in providers_list if p.get("active")), None)
        if active and config_key in active:
            val = active[config_key]
            if val and isinstance(val, str):
                return val
    except Exception as exc:
        logger.debug("get_model_for_role(%r): %s", role, exc)
    return None


def get_small_model(working_dir: Optional[Path] = None) -> Optional[str]:
    """SM-1: Return the configured small/background model name.

    Resolution order (first non-None wins):
    1. ``small_model`` key from the active provider entry in ``providers.json``
    2. ``small_model`` key from the merged config layers (user/workspace overrides)
    3. ``None`` — callers should fall back to the provider's active model

    Parameters
    ----------
    working_dir:
        Forwarded to ``load_merged_config()``.

    Returns
    -------
    str or None
        Model name to use for background LLM calls, or ``None`` to use the
        default active model.
    """
    # 1. Check the active provider entry in providers.json
    try:
        bundled = _BUNDLED_DEFAULTS
        if bundled.is_file():
            raw = bundled.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            providers_list = parsed if isinstance(parsed, list) else []
            active = next((p for p in providers_list if p.get("active")), None)
            if active:
                val = active.get("small_model")
                if val and isinstance(val, str):
                    return val
    except Exception as exc:
        logger.debug("get_small_model: provider lookup failed: %s", exc)

    # 2. Merged config layers (user / workspace overrides)
    cfg = load_merged_config(working_dir)
    val = cfg.get("small_model")
    if val and isinstance(val, str):
        return val

    return None


# ---------------------------------------------------------------------------
# OP-5: Project-level config — .agent-context/config.json
# ---------------------------------------------------------------------------

_PROJECT_CONFIG_FILENAME = "config.json"
_PROJECT_CONFIG_CACHE: Dict[str, tuple] = {}  # path_str → (mtime, config_dict)


def load_project_config(working_dir: str) -> Dict[str, Any]:
    """Load ``.agent-context/config.json`` from *working_dir* if it exists.

    Uses mtime-based caching so hot paths (every LLM call) only hit disk when
    the file has changed since the last read.  Returns ``{}`` on missing or
    invalid JSON — callers never need to guard.

    Schema (all fields optional):

    .. code-block:: json

        {
          "model": "gemma-4-26b-a4b-it",
          "instructions": ["Use pnpm, never npm.", "Tests: .venv/bin/pytest."],
          "tools": {"bash": false, "glob": true},
          "permissions": {"deny_write": ["*.env", "db/migrations/*"]}
        }
    """
    path = Path(working_dir) / ".agent-context" / _PROJECT_CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _PROJECT_CONFIG_CACHE.get(str(path))
    if cached is not None:
        cached_mtime, cached_cfg = cached
        if mtime == cached_mtime:
            return cached_cfg
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
        _PROJECT_CONFIG_CACHE[str(path)] = (mtime, cfg)
        return cfg
    except Exception as exc:
        logger.warning("load_project_config: failed to parse %s: %s", path, exc)
        return {}


def get_project_instructions(working_dir: str) -> List[str]:
    """Return the ``instructions`` list from the project config.

    Each element is a plain-text instruction that will be injected into the
    system prompt as a ``<project_instructions>`` block.
    """
    raw = load_project_config(working_dir).get("instructions", [])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item]


def get_project_tool_overrides(working_dir: str) -> Dict[str, bool]:
    """Return tool enable/disable overrides from the project config.

    Keys are tool names; values are ``True`` (keep enabled) or ``False``
    (deregister before the next task).
    """
    raw = load_project_config(working_dir).get("tools", {})
    if not isinstance(raw, dict):
        return {}
    return {k: bool(v) for k, v in raw.items()}


def get_project_deny_write_patterns(working_dir: str) -> List[str]:
    """Return path glob patterns that must never be written by the agent.

    Patterns are matched against the *relative* path of the target file
    (relative to *working_dir*) using ``fnmatch.fnmatch``.

    Example: ``[".env*", "db/migrations/*"]``
    """
    raw = load_project_config(working_dir).get("permissions", {})
    if not isinstance(raw, dict):
        return []
    deny = raw.get("deny_write", [])
    if not isinstance(deny, list):
        return []
    return [str(p) for p in deny if p]


def get_project_model_override(working_dir: str) -> Optional[str]:
    """Return the per-project model name override, or ``None``.

    When set, this takes precedence over the provider's default model.
    """
    val = load_project_config(working_dir).get("model")
    return str(val) if val and isinstance(val, str) else None
