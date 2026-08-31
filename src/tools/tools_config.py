"""
Tools configuration module.

Provides configurable values used across tool modules.  Override these by
calling ``configure()`` before using any tool, or set the corresponding
environment variables.

Example::

    from src.tools.tools_config import configure, AGENT_CONTEXT_DIR
    configure(context_dir=".coding-agent-state")
"""

from __future__ import annotations

import os
import threading
from enum import Enum
from pathlib import Path
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Per-tool permission levels
# ---------------------------------------------------------------------------


class PermissionLevel(str, Enum):
    """Granularity of access required for a tool call.

    READ_ONLY       — safe reads, no side-effects; always auto-allowed.
    WORKSPACE_WRITE — writes to the working directory; auto-allowed by default.
    DANGER          — destructive or exec operations; requires user approval.
    PROMPT          — always ask the user before executing.
    ALLOW           — unconditionally allow (explicit override).
    """

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGER = "danger"
    PROMPT = "prompt"
    ALLOW = "allow"


# Default permission level for tools not listed in TOOL_PERMISSIONS.
DEFAULT_PERMISSION_LEVEL = PermissionLevel.WORKSPACE_WRITE

# Per-tool permission table.  Override at runtime via set_tool_permission().
TOOL_PERMISSIONS: Dict[str, PermissionLevel] = {
    # --- read-only ---
    "read_file": PermissionLevel.READ_ONLY,
    "glob": PermissionLevel.READ_ONLY,
    "grep": PermissionLevel.READ_ONLY,
    "bash_readonly": PermissionLevel.READ_ONLY,
    "list_files": PermissionLevel.READ_ONLY,
    "find_symbol": PermissionLevel.READ_ONLY,
    "find_references": PermissionLevel.READ_ONLY,
    "search_code": PermissionLevel.READ_ONLY,
    "git_status": PermissionLevel.READ_ONLY,
    "git_log": PermissionLevel.READ_ONLY,
    "git_diff": PermissionLevel.READ_ONLY,
    "read_web_page": PermissionLevel.DANGER,
    "web_search": PermissionLevel.DANGER,
    "get_memory": PermissionLevel.READ_ONLY,
    "load_skill": PermissionLevel.READ_ONLY,
    "list_skills": PermissionLevel.READ_ONLY,
    "summarize_structure": PermissionLevel.READ_ONLY,
    # --- workspace write (default) ---
    "write_file": PermissionLevel.WORKSPACE_WRITE,
    "edit_file_atomic": PermissionLevel.WORKSPACE_WRITE,
    "multiedit": PermissionLevel.WORKSPACE_WRITE,
    "edit_by_line_range": PermissionLevel.WORKSPACE_WRITE,
    "rename_file": PermissionLevel.WORKSPACE_WRITE,
    "delete_file": PermissionLevel.DANGER,
    "save_memory": PermissionLevel.WORKSPACE_WRITE,
    "manage_todo": PermissionLevel.WORKSPACE_WRITE,
    "git_commit": PermissionLevel.WORKSPACE_WRITE,
    "git_stash": PermissionLevel.WORKSPACE_WRITE,
    "git_restore": PermissionLevel.WORKSPACE_WRITE,
    # --- dangerous ---
    "bash": PermissionLevel.DANGER,
    "run_tests": PermissionLevel.DANGER,
    "format_file": PermissionLevel.WORKSPACE_WRITE,
    # --- always prompt ---
    "submit_plan_for_review": PermissionLevel.PROMPT,
    "ask_user": PermissionLevel.PROMPT,
    # CP-15: send_user_message is read_only — it never touches the filesystem
    "send_user_message": PermissionLevel.READ_ONLY,
    # SPAWN-W5: subagent spawning always requires user confirmation (spawn.permission_required)
    "delegate_task": PermissionLevel.PROMPT,
}


def set_tool_permission(tool_name: str, level: PermissionLevel) -> None:
    """Override the permission level for a specific tool at runtime."""
    with _config_lock:
        TOOL_PERMISSIONS[tool_name] = level


def get_tool_permission(tool_name: str) -> PermissionLevel:
    """Return the effective permission level for *tool_name*."""
    with _config_lock:
        return TOOL_PERMISSIONS.get(tool_name, DEFAULT_PERMISSION_LEVEL)


def get_all_tool_permissions() -> Dict[str, PermissionLevel]:
    """Return a shallow copy of the TOOL_PERMISSIONS mapping."""
    with _config_lock:
        return dict(TOOL_PERMISSIONS)


# ---------------------------------------------------------------------------
# Tool name aliases — normalised transparently in execute_tool()
# ---------------------------------------------------------------------------

TOOL_ALIASES: Dict[str, str] = {
    # short read aliases
    "read": "read_file",
    "cat": "read_file",
    "open": "read_file",
    # short write aliases
    "write": "write_file",
    "save": "write_file",
    # short edit aliases
    "edit": "edit_file_atomic",
    "patch": "edit_file_atomic",
    # list / glob
    "ls": "list_files",
    "dir": "list_files",
    "find": "glob",
    # search
    "search": "grep",
    "rg": "grep",
    # shell
    "shell": "bash",
    "run": "bash",
    "cmd": "bash",
    # web
    "fetch": "read_web_page",
    "browse": "read_web_page",
    # fs aliases already registered in registry
    "fs.read": "read_file",
    "fs.write": "write_file",
    "fs.list": "list_files",
}


def resolve_tool_alias(name: str) -> str:
    """Resolve a tool *name* to its canonical name via ``TOOL_ALIASES``.

    Aliases (e.g. ``run`` → ``bash``, ``ls`` → ``list_files``) are normalized
    transparently so permission/policy checks always operate on the canonical
    tool name — preventing deny-rule and approval-gate evasion via aliases.
    Unknown names are returned unchanged.
    """
    return TOOL_ALIASES.get(name, name)


# -----------------------------------------------------------------------
# Module-level state (mutable, not user-facing)
# -----------------------------------------------------------------------

_CONTEXT_DIR: str = ".codingAgent"  # PREFERRED: project-specific context directory
_DEFAULT_WORKDIR: Optional[Path] = None
_AUTONOMOUS_MODE: bool = False  # AUTO-01: global autonomous-mode flag
_ACTIVE_PERMISSION_MODE: Optional[
    PermissionLevel
] = None  # TASK-20: active mode override
# PREV-1: When True, file-write operations (write_file, edit_file_atomic) block
# until the user accepts or rejects the diff preview in the TUI.
# When False (default), writes proceed immediately and the diff is shown as
# informational output only — no Accept/Reject gate.
_REQUIRE_PREVIEW_CONFIRMATION: bool = False

# MED-3 fix: protect all module-global state with a single lock so that
# concurrent tool threads and orchestrator configure() calls don't observe
# torn values (e.g. _AUTONOMOUS_MODE written while _CONTEXT_DIR is being read).
_config_lock: threading.Lock = threading.Lock()


# -----------------------------------------------------------------------
# Public helpers
# -----------------------------------------------------------------------


def configure(
    context_dir: str = ".codingAgent",
    default_workdir: Optional[Path] = None,
    autonomous_mode: bool = False,
    require_preview_confirmation: bool = False,
) -> None:
    """Override default tool configuration.

    Call this **once** at startup, before any tool function is invoked.

    Parameters
    ----------
    context_dir:
        Name of the per-project directory used to store tool state
        (TODO.md, TASK_STATE.md, preferences.md, checkpoints, etc.).
        Default: ``".codingAgent"``.
    default_workdir:
        Default working directory for tool calls that do not explicitly
        pass ``workdir=``.  When *None* the default is the current working
        directory (``Path.cwd()``).
    autonomous_mode:
        When *True* the agent runs without interactive permission prompts.
        Equivalent to passing ``--autonomous`` on the command line.
    require_preview_confirmation:
        When *True*, file-write operations (write_file, edit_file_atomic)
        block and display a diff with Accept / Reject buttons before
        committing the change.  When *False* (default), writes proceed
        immediately and the diff is shown as informational output only.
    """
    global _CONTEXT_DIR, _DEFAULT_WORKDIR, _AUTONOMOUS_MODE, _REQUIRE_PREVIEW_CONFIRMATION
    with _config_lock:
        _CONTEXT_DIR = context_dir
        _DEFAULT_WORKDIR = default_workdir
        _AUTONOMOUS_MODE = autonomous_mode
        _REQUIRE_PREVIEW_CONFIRMATION = require_preview_confirmation


def agent_context_path(workdir: Path) -> Path:
    """Return the full path to the agent-context directory for *workdir*.

    Creates the directory if it does not exist.
    """
    with _config_lock:
        ctx_dir = _CONTEXT_DIR  # Defaults to .codingAgent

    # Always use the configured directory (.codingAgent by default).
    # Legacy directories (.agent-context, .agent) are no longer created or
    # returned — all state is written to .codingAgent only.
    configured = workdir / ctx_dir
    configured.mkdir(parents=True, exist_ok=True)
    return configured


def get_default_workdir() -> Path:
    """Return the default working directory."""
    with _config_lock:
        if _DEFAULT_WORKDIR is not None:
            return _DEFAULT_WORKDIR
    return Path.cwd()


def get_context_dir_name() -> str:
    """Return the configured agent-context directory name."""
    with _config_lock:
        return _CONTEXT_DIR


def get_audit_dir(workdir: Path) -> Path:
    """Return the directory to use for permission audit logs.

    Uses agent_context_path() for consistent behavior.
    """
    return agent_context_path(workdir if workdir else Path.cwd())


# AUTO-01: Autonomous mode helpers
def is_autonomous() -> bool:
    """Return *True* when the agent is running in autonomous (non-interactive) mode.

    In autonomous mode, DANGER-level tools are executed without waiting for
    user approval and PROMPT-level tools are auto-allowed.
    """
    # Also honour the CODINGAGENT_AUTONOMOUS env var for script-level overrides.
    if os.getenv("CODINGAGENT_AUTONOMOUS", "").lower() in ("1", "true", "yes"):
        return True
    with _config_lock:
        return _AUTONOMOUS_MODE


def set_autonomous(enabled: bool = True) -> None:
    """Enable or disable autonomous mode at runtime.

    Prefer calling ``configure(autonomous_mode=True)`` at startup; this
    function exists for dynamic switching (e.g. the TUI lets the user
    toggle the mode mid-session).
    """
    global _AUTONOMOUS_MODE
    with _config_lock:
        _AUTONOMOUS_MODE = enabled


# PREV-1: Preview confirmation helpers
def requires_preview_confirmation() -> bool:
    """Return *True* when file writes must be confirmed by the user before
    being applied.

    When *True*, write_file and edit_file_atomic block on a diff preview gate
    until the TUI user clicks Accept or Reject.  When *False* (default), writes
    proceed immediately and the diff is displayed as informational output only.

    Controlled by the ``preview_confirmation`` key in ``.localAgent/config.json``.
    Can also be toggled at runtime via ``set_require_preview_confirmation()``.
    """
    with _config_lock:
        return _REQUIRE_PREVIEW_CONFIRMATION


def set_require_preview_confirmation(enabled: bool) -> None:
    """Enable or disable the diff-preview confirmation gate at runtime."""
    global _REQUIRE_PREVIEW_CONFIRMATION
    with _config_lock:
        _REQUIRE_PREVIEW_CONFIRMATION = enabled


# TASK-20: Active permission mode helpers
def get_active_permission_mode() -> Optional[PermissionLevel]:
    """Return the active permission mode override, or *None* if not set."""
    with _config_lock:
        return _ACTIVE_PERMISSION_MODE


def set_active_permission_mode(mode: PermissionLevel) -> None:
    """Set the active permission mode at runtime.

    When set, this overrides the per-tool ``TOOL_PERMISSIONS`` table for
    tools whose level is *less restrictive* than *mode*.  For example,
    setting ``PermissionLevel.READ_ONLY`` blocks all write and danger tools
    regardless of their per-tool entry.

    Pass ``None`` to clear the override (not directly supported — call
    ``configure()`` again at startup to reset).
    """
    global _ACTIVE_PERMISSION_MODE
    with _config_lock:
        _ACTIVE_PERMISSION_MODE = mode


def reset_to_defaults() -> None:
    """Reset module-level configuration to the documented defaults.

    This is primarily intended for test code to restore deterministic
    module state between tests and avoid order-dependent failures.
    """
    global _CONTEXT_DIR, _DEFAULT_WORKDIR, _AUTONOMOUS_MODE, _ACTIVE_PERMISSION_MODE, _REQUIRE_PREVIEW_CONFIRMATION
    with _config_lock:
        _CONTEXT_DIR = ".codingAgent"
        _DEFAULT_WORKDIR = None
        _AUTONOMOUS_MODE = False
        _ACTIVE_PERMISSION_MODE = None
        _REQUIRE_PREVIEW_CONFIRMATION = False
