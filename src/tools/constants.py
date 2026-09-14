"""Centralized tool-constant sets (audit PHASE-4 item 4.6).

Single source of truth for the shared tool-classification sets and the
canonical tool-name alias map used by the permission gates, dry-run guard,
loop guards, and execution pipeline.

Modules that previously owned private copies now re-export from here so a
tool's membership can only drift in one place.  This module is a pure-data
leaf: importing it must never have side effects (no logging, no IO, no
mutable runtime state).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Modifying tools (read-before-write / dry-run / loop-guard membership)
# ---------------------------------------------------------------------------

#: Tools that require the target file to have been read before writing.
WRITE_TOOLS_REQUIRING_READ: frozenset[str] = frozenset(
    {
        "edit_file",
        "edit_file_atomic",
        "write_file",
        "edit_by_line_range",
        "apply_patch",
        # Destructive tools — also subject to the _affected_files scope guard
        "delete_file",
        "rename_file",
        "ast_rename",
        "manage_todo",  # SEC-2: previously must stay in sync across modules
    }
)

#: Tools that require a prior successful read call on the target path (the
#: loop-guard flavor of :data:`WRITE_TOOLS_REQUIRING_READ`; computed from the
#: same base so the two can never drift — F-88 keeps ``multiedit`` here).
MODIFYING_TOOLS: frozenset[str] = frozenset(
    WRITE_TOOLS_REQUIRING_READ | {"multiedit"}
)

#: Extra tools blocked in dry-run mode beyond those that require a read:
#: side-effecting exec/network/git ops that cannot be trivially previewed.
_EXEC_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "run_bash",
        "execute_bash",
        "run_command",
        "execute_command",
        "git_commit",
        "git_push",
    }
)
DRY_RUN_BLOCKED_TOOLS: frozenset[str] = frozenset(
    WRITE_TOOLS_REQUIRING_READ | _EXEC_SIDE_EFFECT_TOOLS
)

#: Tools that always require explicit user approval before execution.
PERMISSION_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        "delete_file",
        "run_bash",
    }
)

#: Ordering of permission levels from least to most permissive.
PERM_ORDER: dict[str, int] = {
    "read_only": 0,
    "workspace_write": 1,
    "danger": 2,
    "prompt": 3,
    "allow": 4,
}

# ---------------------------------------------------------------------------
# Filesystem-policy membership
# ---------------------------------------------------------------------------

#: Tools that are auto-approved when every path/workdir arg is inside the
#: project working directory.  ``ask_user`` is always auto-approved regardless
#: of path because it produces no filesystem side-effects.
WORKDIR_SAFE_TOOLS: frozenset[str] = frozenset(
    {"bash", "run_tests", "run_bash", "ask_user"}
)

#: File-touching tool names whose path args should be checked for
#: external-directory access.
FILE_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "read_file_chunk",
        "read_file_bytes",
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "multiedit",
        "delete_file",
        "rename_file",
        "list_dir",
        "glob_files",
    }
)

# ---------------------------------------------------------------------------
# Canonical tool-name aliases
# ---------------------------------------------------------------------------

#: Short-name aliases normalized transparently before any permission/policy
#: check so a deny/approval rule keyed on the canonical name can't be evaded.
TOOL_ALIASES: dict[str, str] = {
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
