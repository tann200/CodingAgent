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
    """Return the path to memory.md (global, per-user agent memory)."""
    return get_data_dir() / "memory.md"


def get_user_preferences_path(workdir: Optional[Path] = None) -> Path:
    """Return the path to preferences.md (project-specific user preferences).

    This stores user preferences, working style, and explicit instructions
    that are specific to each project. Stored in the project's .codingAgent/
    directory (project-specific) rather than global ~/.coding_agent/ (global).

    Args:
        workdir: The project working directory. Defaults to cwd.
    """
    from src.tools.tools_config import agent_context_path

    wd = workdir if workdir else Path.cwd()
    return agent_context_path(wd) / "preferences.md"


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
    """Return the agent context directory for the current working directory.

    Always returns (and creates if necessary) the configured canonical
    directory — `.codingAgent` by default.  Legacy directories such as
    `.agent-context` and `.agent` are no longer consulted; all state is
    stored in `.codingAgent` only.

    Use `agent_context_path()` from tools_config for explicit workdir support.
    """
    try:
        from src.tools.tools_config import agent_context_path

        return agent_context_path(Path.cwd())
    except Exception:
        ctx_dir_name = os.getenv("CODINGAGENT_CONTEXT_DIR") or ".codingAgent"
        candidate = Path.cwd() / ctx_dir_name
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
