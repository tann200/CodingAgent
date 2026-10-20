"""_capabilities.py — Canonical capability metadata and target tool contracts (Track 1).

Defines the authoritative 27 model-facing capability tools, strict schemas,
operation enums, tier assignments, permissions, and legacy adapter mappings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from src.tools._tool import PermissionKind


# ---------------------------------------------------------------------------
# Capability Descriptor
# ---------------------------------------------------------------------------


class CapabilityTier(str, Enum):
    MEDIUM_CORE = "medium"
    FRONTIER = "frontier"


@dataclass(frozen=True)
class CapabilityDescriptor:
    """Authoritative contract for one model-facing capability tool."""

    name: str
    operations: Tuple[str, ...]
    description: str
    permission_kind: PermissionKind
    operation_permissions: Dict[str, PermissionKind] = field(default_factory=dict)
    requires_approval: bool = False
    requires_sandbox: bool = False
    workspace_confinement: bool = True
    rollback_strategy: str = "none"  # "snapshot", "transaction", "git", "not_revertible", "none"
    output_budget_chars: int = 16384
    audit_classification: str = "read"  # "read", "write", "exec", "network", "admin"
    tier: CapabilityTier = CapabilityTier.MEDIUM_CORE
    schema: Dict[str, Any] = field(default_factory=dict)

    def get_permission_kind(self, operation: Optional[str] = None) -> PermissionKind:
        """Resolve operation-specific permission kind, falling back to default."""
        if operation and operation in self.operation_permissions:
            return self.operation_permissions[operation]
        return self.permission_kind


# ---------------------------------------------------------------------------
# The 27 Canonical Capabilities (Audit Appendix §953-982)
# ---------------------------------------------------------------------------

_CAPABILITY_DEFINITIONS: List[CapabilityDescriptor] = [
    # 1. files_read
    CapabilityDescriptor(
        name="files_read",
        operations=("read", "chunk", "bytes", "tail"),
        description="Read file contents with support for whole-file, line-chunk, byte-range, and tail reading.",
        permission_kind=PermissionKind.READ_FILE,
        audit_classification="read",
        tier=CapabilityTier.MEDIUM_CORE,
        output_budget_chars=32768,
        schema={
            "type": "function",
            "function": {
                "name": "files_read",
                "description": "Read file contents from the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative or absolute file path."},
                        "operation": {
                            "type": "string",
                            "enum": ["read", "chunk", "bytes", "tail"],
                            "description": "Read operation mode (default: 'read').",
                        },
                        "offset": {"type": "integer", "description": "Starting line (chunk) or byte offset (bytes)."},
                        "limit": {"type": "integer", "description": "Number of lines (chunk) or byte count (bytes)."},
                        "lines": {"type": "integer", "description": "Number of lines to read from end (tail mode)."},
                    },
                    "required": ["path"],
                },
            },
        },
    ),
    # 2. files_list
    CapabilityDescriptor(
        name="files_list",
        operations=("list", "glob", "find"),
        description="List files in a directory or match files using glob or recursive pattern search.",
        permission_kind=PermissionKind.READ_FILE,
        audit_classification="read",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "files_list",
                "description": "List directory contents or find files matching glob patterns.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path to list (default: current)."},
                        "operation": {
                            "type": "string",
                            "enum": ["list", "glob", "find"],
                            "description": "Listing mode (default: 'list').",
                        },
                        "pattern": {"type": "string", "description": "Glob or match pattern."},
                        "max_results": {"type": "integer", "description": "Maximum number of results to return."},
                    },
                },
            },
        },
    ),
    # 3. files_search
    CapabilityDescriptor(
        name="files_search",
        operations=("grep", "code"),
        description="Search for text or regex patterns in workspace files.",
        permission_kind=PermissionKind.READ_FILE,
        audit_classification="read",
        tier=CapabilityTier.MEDIUM_CORE,
        output_budget_chars=24576,
        schema={
            "type": "function",
            "function": {
                "name": "files_search",
                "description": "Search for text patterns across workspace files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Search pattern or regex."},
                        "path": {"type": "string", "description": "Directory or file path to search."},
                        "operation": {
                            "type": "string",
                            "enum": ["grep", "code"],
                            "description": "Search mode (default: 'grep').",
                        },
                        "include": {"type": "string", "description": "Glob pattern for included files (e.g. '*.py')."},
                        "max_results": {"type": "integer", "description": "Max matching lines/entries to return."},
                    },
                    "required": ["pattern"],
                },
            },
        },
    ),
    # 4. code_search
    CapabilityDescriptor(
        name="code_search",
        operations=("search",),
        description="Semantic and cross-language code search using indexed repository intelligence.",
        permission_kind=PermissionKind.READ_FILE,
        audit_classification="read",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "code_search",
                "description": "Search code snippets and definitions across repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language or code query."},
                        "max_results": {"type": "integer", "description": "Max snippets to return."},
                    },
                    "required": ["query"],
                },
            },
        },
    ),
    # 5. symbols
    CapabilityDescriptor(
        name="symbols",
        operations=("find", "references", "list"),
        description="Inspect code symbols (functions, classes, methods) and cross-references.",
        permission_kind=PermissionKind.READ_FILE,
        audit_classification="read",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "symbols",
                "description": "Find symbols, references, or list symbols in a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Symbol name to look up."},
                        "path": {"type": "string", "description": "File path when listing symbols in a file."},
                        "operation": {
                            "type": "string",
                            "enum": ["find", "references", "list"],
                            "description": "Symbol query mode (default: 'find').",
                        },
                    },
                },
            },
        },
    ),
    # 6. repo_inspect
    CapabilityDescriptor(
        name="repo_inspect",
        operations=("overview", "summary", "structure", "fingerprint", "analyze"),
        description="Inspect repository high-level architecture, directory layout, and tech stack fingerprint.",
        permission_kind=PermissionKind.READ_FILE,
        audit_classification="read",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "repo_inspect",
                "description": "Inspect repository architecture, summaries, or technology fingerprint.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["overview", "summary", "structure", "fingerprint", "analyze"],
                            "description": "Inspection mode (default: 'overview').",
                        },
                        "workdir": {"type": "string", "description": "Workspace root directory."},
                        "max_depth": {"type": "integer", "description": "Maximum tree depth for structure."},
                    },
                },
            },
        },
    ),
    # 7. files_write
    CapabilityDescriptor(
        name="files_write",
        operations=("write", "create_dir"),
        description="Write full file content atomically or create directories within workspace.",
        permission_kind=PermissionKind.WRITE_FILE,
        rollback_strategy="snapshot",
        audit_classification="write",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "files_write",
                "description": "Write file contents atomically or create directories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to create or write."},
                        "content": {"type": "string", "description": "Content to write to file."},
                        "operation": {
                            "type": "string",
                            "enum": ["write", "create_dir"],
                            "description": "Write mode (default: 'write').",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
    ),
    # 8. files_edit
    CapabilityDescriptor(
        name="files_edit",
        operations=("exact", "range", "multi"),
        description="Perform precise string replacements, line-range replacements, or atomic multi-edits.",
        permission_kind=PermissionKind.WRITE_FILE,
        rollback_strategy="snapshot",
        audit_classification="write",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "files_edit",
                "description": "Edit existing file using exact text replacement or line ranges.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the file to edit."},
                        "operation": {
                            "type": "string",
                            "enum": ["exact", "range", "multi"],
                            "description": "Edit mode (default: 'exact').",
                        },
                        "old_str": {"type": "string", "description": "Exact text to replace."},
                        "new_str": {"type": "string", "description": "Replacement text."},
                        "start_line": {"type": "integer", "description": "1-based starting line (range mode)."},
                        "end_line": {"type": "integer", "description": "1-based ending line (range mode)."},
                        "new_content": {"type": "string", "description": "New content for range mode."},
                        "edits": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "List of {old_str, new_str} dicts (multi mode).",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
    ),
    # 9. files_move
    CapabilityDescriptor(
        name="files_move",
        operations=("move",),
        description="Rename or move files within the workspace. Destination must not exist unless confirmed.",
        permission_kind=PermissionKind.WRITE_FILE,
        rollback_strategy="snapshot",
        audit_classification="write",
        tier=CapabilityTier.FRONTIER,
        schema={
            "type": "function",
            "function": {
                "name": "files_move",
                "description": "Move or rename a file within the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source file path."},
                        "destination": {"type": "string", "description": "Destination file path."},
                        "overwrite": {"type": "boolean", "description": "Whether to overwrite destination."},
                    },
                    "required": ["source", "destination"],
                },
            },
        },
    ),
    # 10. files_delete
    CapabilityDescriptor(
        name="files_delete",
        operations=("delete",),
        description="Delete a file within the workspace with safety checks and snapshot rollback.",
        permission_kind=PermissionKind.WRITE_FILE,
        requires_approval=True,
        rollback_strategy="snapshot",
        audit_classification="admin",
        tier=CapabilityTier.FRONTIER,
        schema={
            "type": "function",
            "function": {
                "name": "files_delete",
                "description": "Delete a file within the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the file to delete."},
                    },
                    "required": ["path"],
                },
            },
        },
    ),
    # 11. patch
    CapabilityDescriptor(
        name="patch",
        operations=("preview", "apply"),
        description="Generate unified diff patch previews or apply validated unified patches atomically.",
        permission_kind=PermissionKind.WRITE_FILE,
        operation_permissions={
            "preview": PermissionKind.NONE,
            "apply": PermissionKind.WRITE_FILE,
        },
        rollback_strategy="snapshot",
        audit_classification="write",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "patch",
                "description": "Generate or apply a unified diff patch.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patch": {"type": "string", "description": "Unified diff patch string to apply."},
                        "path": {"type": "string", "description": "Target file path (for preview/apply)."},
                        "new_content": {"type": "string", "description": "New content for preview generation."},
                        "operation": {
                            "type": "string",
                            "enum": ["preview", "apply"],
                            "description": "Patch mode (default: 'apply').",
                        },
                    },
                },
            },
        },
    ),
    # 12. shell
    CapabilityDescriptor(
        name="shell",
        operations=("execute", "readonly"),
        description="Execute commands in a secure sandbox with timeout, process-group confinement, and logging.",
        permission_kind=PermissionKind.EXECUTE_BASH,
        operation_permissions={
            "readonly": PermissionKind.NONE,
            "execute": PermissionKind.EXECUTE_BASH,
        },
        requires_approval=True,
        requires_sandbox=True,
        rollback_strategy="not_revertible",
        audit_classification="exec",
        tier=CapabilityTier.MEDIUM_CORE,
        output_budget_chars=32768,
        schema={
            "type": "function",
            "function": {
                "name": "shell",
                "description": "Execute shell commands inside the workspace sandbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command string to execute."},
                        "operation": {
                            "type": "string",
                            "enum": ["execute", "readonly"],
                            "description": "Execution mode (default: 'execute').",
                        },
                        "timeout": {"type": "integer", "description": "Timeout in seconds (default: 60)."},
                    },
                    "required": ["command"],
                },
            },
        },
    ),
    # 13. tasks_wait
    CapabilityDescriptor(
        name="tasks_wait",
        operations=("check", "wait"),
        description="Poll or wait for background agent-owned tasks to complete.",
        permission_kind=PermissionKind.NONE,
        audit_classification="read",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "tasks_wait",
                "description": "Check status or wait for a background task.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "ID of the background task."},
                        "operation": {
                            "type": "string",
                            "enum": ["check", "wait"],
                            "description": "Wait mode (default: 'check').",
                        },
                    },
                    "required": ["task_id"],
                },
            },
        },
    ),
    # 14. verify
    CapabilityDescriptor(
        name="verify",
        operations=("test", "lint", "syntax"),
        description="Run test suites, linters, or syntax checks and return bounded pass/fail evidence.",
        permission_kind=PermissionKind.EXECUTE_BASH,
        operation_permissions={
            "test": PermissionKind.EXECUTE_BASH,
            "lint": PermissionKind.NONE,
            "syntax": PermissionKind.NONE,
        },
        audit_classification="exec",
        tier=CapabilityTier.MEDIUM_CORE,
        output_budget_chars=24576,
        schema={
            "type": "function",
            "function": {
                "name": "verify",
                "description": "Run tests, linter, or syntax checks to verify workspace state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["test", "lint", "syntax"],
                            "description": "Verification mode (default: 'test').",
                        },
                        "path": {"type": "string", "description": "Optional target file or directory."},
                        "test_name": {"type": "string", "description": "Specific test or filter."},
                    },
                },
            },
        },
    ),
    # 15. format
    CapabilityDescriptor(
        name="format",
        operations=("format", "preview"),
        description="Auto-format code or preview code style fixes across modified files.",
        permission_kind=PermissionKind.WRITE_FILE,
        rollback_strategy="snapshot",
        audit_classification="write",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "format",
                "description": "Format file or preview formatting diffs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to file to format."},
                        "operation": {
                            "type": "string",
                            "enum": ["format", "preview"],
                            "description": "Format mode (default: 'format').",
                        },
                    },
                    "required": ["file_path"],
                },
            },
        },
    ),
    # 16. git
    CapabilityDescriptor(
        name="git",
        operations=("status", "log", "diff", "commit", "stash", "restore"),
        description="Inspect git history and status, or create commits, stashes, and restores.",
        permission_kind=PermissionKind.GIT_READ,
        operation_permissions={
            "status": PermissionKind.GIT_READ,
            "log": PermissionKind.GIT_READ,
            "diff": PermissionKind.GIT_READ,
            "commit": PermissionKind.GIT_WRITE,
            "stash": PermissionKind.GIT_WRITE,
            "restore": PermissionKind.GIT_WRITE,
        },
        audit_classification="exec",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "git",
                "description": "Inspect git status/diff or manage commits, stash, and restore.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["status", "log", "diff", "commit", "stash", "restore"],
                            "description": "Git operation mode (default: 'status').",
                        },
                        "message": {"type": "string", "description": "Commit or stash message."},
                        "path": {"type": "string", "description": "File path for diff/restore."},
                        "max_count": {"type": "integer", "description": "Max commits for log."},
                    },
                },
            },
        },
    ),
    # 17. lsp
    CapabilityDescriptor(
        name="lsp",
        operations=("diagnostics", "definition", "references", "symbols", "hover", "rename"),
        description="Query language server for type diagnostics, hover doc, definitions, references, and safe rename.",
        permission_kind=PermissionKind.LSP_READ,
        operation_permissions={
            "diagnostics": PermissionKind.LSP_READ,
            "definition": PermissionKind.LSP_READ,
            "references": PermissionKind.LSP_READ,
            "symbols": PermissionKind.LSP_READ,
            "hover": PermissionKind.LSP_READ,
            "rename": PermissionKind.LSP_WRITE,
        },
        rollback_strategy="snapshot",
        audit_classification="read",
        tier=CapabilityTier.FRONTIER,
        schema={
            "type": "function",
            "function": {
                "name": "lsp",
                "description": "Language server queries (diagnostics, definition, symbols, hover, rename).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File containing the symbol or target."},
                        "operation": {
                            "type": "string",
                            "enum": ["diagnostics", "definition", "references", "symbols", "hover", "rename"],
                            "description": "LSP query mode (default: 'diagnostics').",
                        },
                        "line": {"type": "integer", "description": "0-based line number."},
                        "col": {"type": "integer", "description": "0-based column number."},
                        "new_name": {"type": "string", "description": "New symbol name for rename mode."},
                    },
                    "required": ["path"],
                },
            },
        },
    ),
    # 18. web_search
    CapabilityDescriptor(
        name="web_search",
        operations=("search",),
        description="Search the web for up-to-date documentation, API specs, and technical information.",
        permission_kind=PermissionKind.NETWORK,
        audit_classification="network",
        tier=CapabilityTier.FRONTIER,
        schema={
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for technical documentation and answers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Web search query."},
                        "max_results": {"type": "integer", "description": "Number of results to return."},
                    },
                    "required": ["query"],
                },
            },
        },
    ),
    # 19. web_read
    CapabilityDescriptor(
        name="web_read",
        operations=("read",),
        description="Fetch and extract readable content from a remote web URL with SSRF protection.",
        permission_kind=PermissionKind.NETWORK,
        audit_classification="network",
        tier=CapabilityTier.FRONTIER,
        output_budget_chars=32768,
        schema={
            "type": "function",
            "function": {
                "name": "web_read",
                "description": "Fetch web page content with SSRF and redirect validation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL of the web page to read."},
                    },
                    "required": ["url"],
                },
            },
        },
    ),
    # 20. memory
    CapabilityDescriptor(
        name="memory",
        operations=("search", "save", "mistake"),
        description="Store and recall project lessons, user preferences, and mistake prevention rules.",
        permission_kind=PermissionKind.MEMORY,
        operation_permissions={
            "search": PermissionKind.NONE,
            "save": PermissionKind.MEMORY,
            "mistake": PermissionKind.MEMORY,
        },
        audit_classification="write",
        tier=CapabilityTier.FRONTIER,
        schema={
            "type": "function",
            "function": {
                "name": "memory",
                "description": "Search or store long-term project memories and lessons.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["search", "save", "mistake"],
                            "description": "Memory mode (default: 'search').",
                        },
                        "query": {"type": "string", "description": "Search query for memory recall."},
                        "content": {"type": "string", "description": "Memory content or rule to save."},
                    },
                },
            },
        },
    ),
    # 21. state
    CapabilityDescriptor(
        name="state",
        operations=("checkpoint", "list", "diff", "revert"),
        description="Manage workspace state checkpoints, inspect undo diffs, or revert tool side-effects.",
        permission_kind=PermissionKind.WRITE_FILE,
        operation_permissions={
            "checkpoint": PermissionKind.WRITE_FILE,
            "list": PermissionKind.NONE,
            "diff": PermissionKind.NONE,
            "revert": PermissionKind.WRITE_FILE,
        },
        audit_classification="admin",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "state",
                "description": "Manage checkpoints, list snapshots, inspect diff, or revert changes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["checkpoint", "list", "diff", "revert"],
                            "description": "State management mode (default: 'checkpoint').",
                        },
                        "name": {"type": "string", "description": "Checkpoint name or description."},
                    },
                },
            },
        },
    ),
    # 22. todo
    CapabilityDescriptor(
        name="todo",
        operations=("list", "add", "update", "complete", "remove"),
        description="Track task items and todo lists scoped to the current coding session.",
        permission_kind=PermissionKind.WRITE_FILE,
        audit_classification="write",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "todo",
                "description": "Manage session todo list and task milestones.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["list", "add", "update", "complete", "remove"],
                            "description": "Todo operation mode (default: 'list').",
                        },
                        "text": {"type": "string", "description": "Task description for add/update."},
                        "task_id": {"type": "string", "description": "Task ID for update/complete/remove."},
                    },
                },
            },
        },
    ),
    # 23. skills
    CapabilityDescriptor(
        name="skills",
        operations=("list", "load"),
        description="Inspect and load domain-specific agent skills.",
        permission_kind=PermissionKind.NONE,
        audit_classification="read",
        tier=CapabilityTier.FRONTIER,
        schema={
            "type": "function",
            "function": {
                "name": "skills",
                "description": "List available skills or load instructions for a skill.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["list", "load"],
                            "description": "Skill operation mode (default: 'list').",
                        },
                        "name": {"type": "string", "description": "Skill name to load."},
                    },
                },
            },
        },
    ),
    # 24. user_interact
    CapabilityDescriptor(
        name="user_interact",
        operations=("ask", "notify"),
        description="Ask the user a clarifying question or send a progress notification message.",
        permission_kind=PermissionKind.NONE,
        operation_permissions={
            "ask": PermissionKind.NONE,
            "notify": PermissionKind.NONE,
        },
        requires_approval=False,
        audit_classification="read",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "user_interact",
                "description": "Prompt user for guidance or send an informational progress notification.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Message or question for the user."},
                        "operation": {
                            "type": "string",
                            "enum": ["ask", "notify"],
                            "description": "Interaction mode (default: 'ask').",
                        },
                    },
                    "required": ["message"],
                },
            },
        },
    ),
    # 25. delegate
    CapabilityDescriptor(
        name="delegate",
        operations=("spawn", "parallel"),
        description="Spawn specialized sub-agents to perform focused or parallel subtasks.",
        permission_kind=PermissionKind.DELEGATE,
        requires_approval=True,
        audit_classification="admin",
        tier=CapabilityTier.FRONTIER,
        schema={
            "type": "function",
            "function": {
                "name": "delegate",
                "description": "Delegate a task to an autonomous sub-agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Subtask instruction for delegate."},
                        "operation": {
                            "type": "string",
                            "enum": ["spawn", "parallel"],
                            "description": "Delegation mode (default: 'spawn').",
                        },
                        "role": {"type": "string", "description": "Role for the sub-agent."},
                    },
                    "required": ["task"],
                },
            },
        },
    ),
    # 26. plan
    CapabilityDescriptor(
        name="plan",
        operations=("enter", "exit", "submit"),
        description="Enter or exit plan mode, or submit a structured plan for human review.",
        permission_kind=PermissionKind.PLAN,
        audit_classification="admin",
        tier=CapabilityTier.MEDIUM_CORE,
        schema={
            "type": "function",
            "function": {
                "name": "plan",
                "description": "Manage planning mode or submit a plan for user review.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["enter", "exit", "submit"],
                            "description": "Plan operation mode (default: 'enter').",
                        },
                        "plan": {"type": "string", "description": "Plan details when submitting for review."},
                    },
                },
            },
        },
    ),
    # 27. batch
    CapabilityDescriptor(
        name="batch",
        operations=("batch",),
        description="Execute multiple independent read-only tool calls concurrently.",
        permission_kind=PermissionKind.NONE,
        audit_classification="read",
        tier=CapabilityTier.FRONTIER,
        schema={
            "type": "function",
            "function": {
                "name": "batch",
                "description": "Execute a batch of read-only tool calls.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "calls": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "List of tool call objects: [{'name': ..., 'arguments': ...}].",
                        },
                    },
                    "required": ["calls"],
                },
            },
        },
    ),
]

# Map name -> CapabilityDescriptor
CAPABILITIES: Dict[str, CapabilityDescriptor] = {
    c.name: c for c in _CAPABILITY_DEFINITIONS
}

# Explicit Tier Sets
MEDIUM_CORE_CAPABILITIES: FrozenSet[str] = frozenset(
    c.name for c in _CAPABILITY_DEFINITIONS if c.tier == CapabilityTier.MEDIUM_CORE
)

FRONTIER_FULL_CAPABILITIES: FrozenSet[str] = frozenset(
    c.name for c in _CAPABILITY_DEFINITIONS
)


# ---------------------------------------------------------------------------
# Legacy Adapters: Map 70+ legacy names to (capability_name, operation)
# ---------------------------------------------------------------------------

LEGACY_TO_CAPABILITY: Dict[str, Tuple[str, str]] = {
    # files_read
    "read_file": ("files_read", "read"),
    "read_file_chunk": ("files_read", "chunk"),
    "read_file_bytes": ("files_read", "bytes"),
    "batched_file_read": ("files_read", "read"),
    "tail_log_file": ("files_read", "tail"),
    "read": ("files_read", "read"),
    "cat": ("files_read", "read"),
    "open": ("files_read", "read"),
    "fs.read": ("files_read", "read"),
    # files_list
    "list_files": ("files_list", "list"),
    "list_dir": ("files_list", "list"),
    "glob": ("files_list", "glob"),
    "glob_files": ("files_list", "glob"),
    "find_files": ("files_list", "find"),
    "ls": ("files_list", "list"),
    "dir": ("files_list", "list"),
    "find": ("files_list", "glob"),
    "fs.list": ("files_list", "list"),
    # files_search
    "grep": ("files_search", "grep"),
    "search": ("files_search", "grep"),
    "rg": ("files_search", "grep"),
    # code_search
    "search_code": ("code_search", "search"),
    # symbols
    "find_symbol": ("symbols", "find"),
    "find_references": ("symbols", "references"),
    "ast_list_symbols": ("symbols", "list"),
    # repo_inspect
    "repo_overview": ("repo_inspect", "overview"),
    "multi_file_summary": ("repo_inspect", "summary"),
    "summarize_structure": ("repo_inspect", "structure"),
    "summarize_structure_detailed": ("repo_inspect", "structure"),
    "fingerprint_tech_stack": ("repo_inspect", "fingerprint"),
    "analyze_repository": ("repo_inspect", "analyze"),
    "initialize_repo_intelligence": ("repo_inspect", "analyze"),
    # files_write
    "write_file": ("files_write", "write"),
    "create_directory": ("files_write", "create_dir"),
    "write": ("files_write", "write"),
    "save": ("files_write", "write"),
    "fs.write": ("files_write", "write"),
    # files_edit
    "edit_file": ("files_edit", "exact"),
    "edit_file_atomic": ("files_edit", "exact"),
    "edit_by_line_range": ("files_edit", "range"),
    "multiedit": ("files_edit", "multi"),
    "edit": ("files_edit", "exact"),
    # files_move
    "rename_file": ("files_move", "move"),
    # files_delete
    "delete_file": ("files_delete", "delete"),
    # patch
    "generate_patch": ("patch", "preview"),
    "apply_patch": ("patch", "apply"),
    "edit_code_block": ("patch", "apply"),
    # shell
    "bash": ("shell", "execute"),
    "bash_readonly": ("shell", "readonly"),
    "run_bash": ("shell", "execute"),
    "execute_bash": ("shell", "execute"),
    "run_command": ("shell", "execute"),
    "execute_command": ("shell", "execute"),
    "run": ("shell", "execute"),
    "cmd": ("shell", "execute"),
    # tasks_wait
    "check_background_task": ("tasks_wait", "check"),
    # verify
    "run_tests": ("verify", "test"),
    "run_tests_legacy": ("verify", "test"),
    "run_js_tests": ("verify", "test"),
    "run_linter": ("verify", "lint"),
    "run_eslint": ("verify", "lint"),
    "run_ts_check": ("verify", "lint"),
    "syntax_check": ("verify", "syntax"),
    # format
    "format_file": ("format", "format"),
    # git
    "git_status": ("git", "status"),
    "git_log": ("git", "log"),
    "git_diff": ("git", "diff"),
    "get_git_diff": ("git", "diff"),
    "git_commit": ("git", "commit"),
    "git_stash": ("git", "stash"),
    "git_restore": ("git", "restore"),
    # lsp
    "lsp_diagnostics": ("lsp", "diagnostics"),
    "lsp_references": ("lsp", "references"),
    "lsp_definition": ("lsp", "definition"),
    "lsp_symbols": ("lsp", "symbols"),
    "lsp_hover": ("lsp", "hover"),
    "lsp_rename": ("lsp", "rename"),
    # web_search
    "web_search": ("web_search", "search"),
    # web_read
    "read_web_page": ("web_read", "read"),
    "fetch": ("web_read", "read"),
    "browse": ("web_read", "read"),
    # memory
    "get_memory": ("memory", "search"),
    "save_memory": ("memory", "save"),
    "memory_search": ("memory", "search"),
    "record_mistake": ("memory", "mistake"),
    # state
    "create_state_checkpoint": ("state", "checkpoint"),
    "list_checkpoints": ("state", "list"),
    "list_snapshots": ("state", "list"),
    "diff_state": ("state", "diff"),
    "revert_last_tool": ("state", "revert"),
    # todo
    "manage_todo": ("todo", "list"),
    # skills
    "list_skills": ("skills", "list"),
    "load_skill": ("skills", "load"),
    # user_interact
    "ask_user": ("user_interact", "ask"),
    "send_user_message": ("user_interact", "notify"),
    # delegate
    "delegate_task": ("delegate", "spawn"),
    "delegate_tasks_parallel": ("delegate", "parallel"),
    # plan
    "enter_plan_mode": ("plan", "enter"),
    "exit_plan_mode": ("plan", "exit"),
    "submit_plan_for_review": ("plan", "submit"),
    # batch
    # ("batch" is already canonical)
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_capability(name: str) -> Optional[CapabilityDescriptor]:
    """Retrieve the CapabilityDescriptor for a target capability name."""
    return CAPABILITIES.get(name)


def resolve_to_capability(
    tool_name: str, arguments: Optional[Dict[str, Any]] = None
) -> Tuple[str, str, Dict[str, Any]]:
    """Resolve a legacy or capability tool call to (target_capability, operation, normalized_args).

    Ensures that legacy callers, MCP servers, and persisted sessions transparently
    dispatch into the 27 capability contracts with the correct operation set.
    """
    args = dict(arguments or {})
    if tool_name in CAPABILITIES:
        cap = CAPABILITIES[tool_name]
        op = args.get("operation") or (cap.operations[0] if cap.operations else "default")
        if "operation" not in args:
            args["operation"] = op
        return tool_name, op, args

    # Check legacy mapping
    if tool_name in LEGACY_TO_CAPABILITY:
        cap_name, default_op = LEGACY_TO_CAPABILITY[tool_name]
        if "operation" not in args:
            args["operation"] = default_op
        return cap_name, args.get("operation", default_op), args

    # Unrecognized or MCP tool
    return tool_name, args.get("operation", "default"), args


def get_schemas_for_tier(tier: str = "medium") -> List[Dict[str, Any]]:
    """Return OpenAI function-calling schemas for the requested model tier."""
    allowed = (
        FRONTIER_FULL_CAPABILITIES
        if tier.lower() == "frontier"
        else MEDIUM_CORE_CAPABILITIES
    )
    return [
        CAPABILITIES[name].schema
        for name in sorted(allowed)
        if name in CAPABILITIES and CAPABILITIES[name].schema
    ]
