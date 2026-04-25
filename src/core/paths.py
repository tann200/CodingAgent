"""Cross-platform path utilities for CodingAgent.

Provides consistent paths for config, data, and cache directories
that work on both Windows and Unix-like systems.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def get_data_dir() -> Path:
    """Return the main data directory for CodingAgent.

    Windows: %LOCALAPPDATA%/CodingAgent
    Unix/macOS: ~/.coding_agent
    """
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "CodingAgent"
    return Path.home() / ".coding_agent"


def get_config_dir() -> Path:
    """Return the config directory.

    Windows: %LOCALAPPDATA%/CodingAgent
    Unix/macOS: ~/.coding_agent
    """
    return get_data_dir()


def get_cache_dir() -> Path:
    """Return the cache directory.

    Windows: %LOCALAPPDATA%/CodingAgent/cache
    Unix/macOS: ~/.coding_agent/cache
    """
    return get_data_dir() / "cache"


def get_sessions_dir() -> Path:
    """Return the sessions directory for session snapshots."""
    return get_data_dir() / "sessions"


def get_hooks_path() -> Path:
    """Return the path to hooks.json."""
    return get_data_dir() / "hooks.json"


def get_permissions_path() -> Path:
    """Return the path to permissions.json."""
    return get_data_dir() / "permissions.json"


def get_prefs_path() -> Path:
    """Return the path to prefs.json."""
    return get_config_dir() / "prefs.json"


def get_user_config_path() -> Path:
    """Return the path to the user-level config.json."""
    return get_config_dir() / "config.json"


def get_memory_path() -> Path:
    """Return the path to memory.md."""
    return get_data_dir() / "memory.md"


def get_events_db_path() -> Path:
    """Return the path to events.db."""
    return get_data_dir() / "events.db"


def get_agents_path() -> Path:
    """Return the path to agents.json."""
    return get_data_dir() / "agents.json"


def get_snapshots_dir(project_id: Optional[str] = None) -> Path:
    """Return the snapshots directory.

    If project_id is provided, returns snapshots for that project.
    Otherwise returns the base snapshots directory.
    """
    base = get_data_dir() / "snapshots"
    if project_id:
        return base / project_id
    return base


def get_skills_cache_dir() -> Path:
    """Return the directory for cached remote skills."""
    return get_cache_dir() / "skills"


# For backwards compatibility - create Path.home() based paths that can be imported
# These are kept for files that don't use the utility functions yet
def _legacy_coding_agent_dir() -> Path:
    """Legacy function - use get_data_dir() instead."""
    return get_data_dir()


def get_agent_context_dir() -> Path:
    """Return the agent context directory.

    This is the directory where agent-specific state is stored.
    Preference order for the directory name:
      1. `src.tools.tools_config.get_context_dir_name()` if available
      2. `CODINGAGENT_CONTEXT_DIR` environment variable
      3. Default: ".localAgent"

    For backwards compatibility, if the chosen directory does not exist but
    one of the legacy directories (".agent-context", ".agent") exists in the
    current working directory, that existing directory is returned instead.
    The returned path is guaranteed to exist (created if necessary).
    """
    # Prefer runtime configuration from tools_config when possible. Import
    # locally to avoid impacting module import order for callers that import
    # this module early.
    try:
        from src.tools.tools_config import get_context_dir_name

        ctx_dir_name = get_context_dir_name()
    except Exception:
        ctx_dir_name = os.getenv("CODINGAGENT_CONTEXT_DIR") or ".localAgent"

    cwd = Path.cwd()
    candidate = cwd / ctx_dir_name

    # If the configured candidate already exists, use it.
    if candidate.exists():
        return candidate

    # Backwards compatibility: prefer existing legacy directories if present.
    for legacy in (cwd / ".agent-context", cwd / ".agent"):
        if legacy.exists():
            return legacy

    # Otherwise create the configured candidate and return it.
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate
