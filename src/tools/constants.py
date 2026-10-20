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
        "edit_code_block",
        "apply_patch",
        "multiedit",
        # Destructive tools — also subject to the _affected_files scope guard
        "delete_file",
        "rename_file",
        "ast_rename",
        "lsp_rename",
        "format_file",
        "manage_todo",  # SEC-2: previously must stay in sync across modules
        # Canonical capability tools (Track 1)
        "files_write",
        "files_edit",
        "files_move",
        "files_delete",
        "patch",
        "format",
        "todo",
    }
)

#: Tools that require a prior successful read call on the target path (the
#: loop-guard flavor of :data:`WRITE_TOOLS_REQUIRING_READ`; computed from the
#: same base so the two can never drift).
MODIFYING_TOOLS: frozenset[str] = frozenset(
    WRITE_TOOLS_REQUIRING_READ
    | {
        "create_directory",
        "git_commit",
        "git_stash",
        "git_restore",
    }
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
        "git_stash",
        "git_restore",
        "create_directory",
        "run_tests",
        "run_tests_legacy",
        "run_js_tests",
        "delegate_task",
        "delegate_tasks_parallel",
        # Canonical capability tools (Track 1)
        "shell",
        "delegate",
        "verify",
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
        # Canonical capability tools (Track 1)
        "files_delete",
        "shell",
        "delegate",
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
        # Canonical capability tools (Track 1)
        "files_read",
        "files_write",
        "files_edit",
        "files_move",
        "files_delete",
        "patch",
        "format",
    }
)

# ---------------------------------------------------------------------------
# Idempotency classification (audit P3.4)
# ---------------------------------------------------------------------------

#: Tools that are safe to re-execute automatically after a *transport* failure
#: (timeout, dropped connection, 5xx): re-running them converges on the same
#: state.  Deliberately an **allowlist** — every tool absent from this set is
#: treated as non-idempotent, so a newly added tool is safe by default instead
#: of requiring someone to remember to classify it.
#:
#: Two families qualify: pure reads, and writes that *overwrite* a target with a
#: fully-specified value (write_file / edit_file* / apply_patch / multiedit /
#: delete_file / rename_file).  A second `write_file` with the same content is a
#: no-op, and a second `delete_file` reports an already-absent file rather than
#: destroying something else.
#:
#: What is deliberately excluded, and why:
#:   * ``bash`` — arbitrary side effects (`x >> f`, ``git commit``, a POST).
#:   * ``git_commit`` / ``git_stash`` — each invocation creates new history.
#:   * ``manage_todo`` / ``record_mistake`` / ``memory_save`` — append.
#:   * ``delegate_task`` / ``delegate_tasks_parallel`` — spawns a subagent.
#:   * ``create_state_checkpoint`` — mints a new checkpoint per call.
#:   * ``revert_last_tool`` — *pops*, so a second call reverts a second change.
#:   * ``send_user_message`` / ``ask_user`` / ``submit_plan_for_review`` — the
#:     duplicate would be delivered to a human.
IDEMPOTENT_TOOLS: frozenset[str] = frozenset(
    {
        # ---- pure reads -------------------------------------------------
        "read_file",
        "read_file_chunk",
        "read_file_bytes",
        "batched_file_read",
        "list_files",
        "list_dir",
        "list_skills",
        "list_snapshots",
        "list_checkpoints",
        "glob",
        "glob_files",
        "grep",
        "search_code",
        "find_files",
        "find_symbol",
        "find_references",
        "ast_list_symbols",
        "repo_overview",
        "multi_file_summary",
        "summarize_structure",
        "summarize_structure_detailed",
        "fingerprint_tech_stack",
        "analyze_repository",
        "initialize_repo_intelligence",
        "memory_search",
        "git_status",
        "git_diff",
        "git_log",
        "get_git_diff",
        "web_search",
        "read_web_page",
        "check_background_task",
        "diff_state",
        # ---- overwrite-shaped writes ------------------------------------
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "multiedit",
        "generate_patch",
        "delete_file",
        # ---- idempotent verification / test runners ---------------------
        "syntax_check",
        "run_eslint",
        "run_ts_check",
        "run_linter",
        # ---- canonical capability tools (Track 1) ------------------------
        "files_read",
        "files_list",
        "files_search",
        "code_search",
        "symbols",
        "repo_inspect",
        "files_write",
        "files_edit",
        "files_delete",
        "tasks_wait",
        "format",
        "verify",
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
