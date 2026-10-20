"""capability_tools.py — Unified 27 capability dispatchers (Track 1).

Consolidates the legacy 70+ tools behind 27 canonical capability tools
with strict operation enums and bounded output envelopes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools._tool import PermissionKind, err, tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. files_read
# ---------------------------------------------------------------------------


@tool(
    name="files_read",
    description="Read file contents with support for whole-file, line-chunk, byte-range, and tail reading.",
    permission_kind=PermissionKind.READ_FILE,
    tags=["coding", "analysis", "review", "debug"],
)
def files_read(
    path: str,
    operation: str = "read",
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    lines: Optional[int] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Read file contents from workspace."""
    from src.tools.file_tools import (
        read_file,
        read_file_bytes,
        read_file_chunk,
        tail_log_file,
    )

    wd = Path(workdir) if workdir else None
    op = (operation or "read").lower()

    if op == "chunk":
        return read_file_chunk(path=path, offset=offset or 0, limit=limit or 100, workdir=wd)  # type: ignore[arg-type]
    elif op == "bytes":
        return read_file_bytes(path=path, offset=offset or 0, limit=limit or 1024, workdir=wd)  # type: ignore[arg-type]
    elif op == "tail":
        return tail_log_file(path=path, lines=lines or 50, workdir=wd)  # type: ignore[arg-type]
    else:
        return read_file(path=path, workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. files_list
# ---------------------------------------------------------------------------


@tool(
    name="files_list",
    description="List directory contents or find files matching glob patterns.",
    permission_kind=PermissionKind.READ_FILE,
    tags=["coding", "analysis", "review", "debug"],
)
def files_list(
    path: Optional[str] = None,
    operation: str = "list",
    pattern: Optional[str] = None,
    max_results: Optional[int] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """List directory contents or find files matching pattern."""
    from src.tools.file_tools import glob, list_files
    from src.tools.repo_read_tools import find_files

    wd = Path(workdir) if workdir else None
    op = (operation or "list").lower()

    if op == "glob":
        return glob(pattern=pattern or "*", path=path or ".", workdir=wd)  # type: ignore[arg-type]
    elif op == "find":
        return find_files(pattern=pattern or "*", workdir=wd)  # type: ignore[arg-type]
    else:
        return list_files(path=path or ".", workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. files_search
# ---------------------------------------------------------------------------


@tool(
    name="files_search",
    description="Search for text patterns across workspace files.",
    permission_kind=PermissionKind.READ_FILE,
    tags=["coding", "analysis", "review", "debug"],
)
def files_search(
    pattern: str,
    path: Optional[str] = None,
    operation: str = "grep",
    include: Optional[str] = None,
    max_results: Optional[int] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Search for text or regex patterns in workspace files."""
    op = (operation or "grep").lower()
    wd = Path(workdir) if workdir else None

    if op == "code":
        from src.tools.repo_read_tools import search_code

        return search_code(query=pattern, max_results=max_results or 10, workdir=wd)  # type: ignore[arg-type]
    else:
        from src.tools.system_tools import grep

        return grep(
            pattern=pattern,
            path=path or ".",
            include=include,
            max_results=max_results or 50,
            workdir=wd,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 4. code_search
# ---------------------------------------------------------------------------


@tool(
    name="code_search",
    description="Search code snippets and definitions across repository.",
    permission_kind=PermissionKind.READ_FILE,
    tags=["coding", "analysis", "review", "debug"],
)
def code_search(
    query: str,
    max_results: Optional[int] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Semantic and cross-language code search."""
    from src.tools.repo_read_tools import search_code

    wd = Path(workdir) if workdir else None
    return search_code(query=query, max_results=max_results or 10, workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. symbols
# ---------------------------------------------------------------------------


@tool(
    name="symbols",
    description="Find symbols, references, or list symbols in a file.",
    permission_kind=PermissionKind.READ_FILE,
    tags=["coding", "analysis", "review", "debug"],
)
def symbols(
    name: Optional[str] = None,
    path: Optional[str] = None,
    operation: str = "find",
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Inspect code symbols and cross-references."""
    op = (operation or "find").lower()
    wd = Path(workdir) if workdir else None

    if op == "references" and name:
        from src.tools.repo_read_tools import find_references

        return find_references(name=name, workdir=wd)  # type: ignore[arg-type]
    elif op == "list" and path:
        from src.tools.ast_tools import ast_list_symbols

        return ast_list_symbols(path=path, workdir=wd)  # type: ignore[arg-type]
    elif name:
        from src.tools.repo_read_tools import find_symbol

        return find_symbol(name=name, workdir=wd)  # type: ignore[arg-type]
    return err("Either 'name' (for find/references) or 'path' (for list) must be specified.")


# ---------------------------------------------------------------------------
# 6. repo_inspect
# ---------------------------------------------------------------------------


@tool(
    name="repo_inspect",
    description="Inspect repository architecture, summaries, or technology fingerprint.",
    permission_kind=PermissionKind.READ_FILE,
    tags=["coding", "analysis", "review", "debug"],
)
def repo_inspect(
    operation: str = "overview",
    workdir: Optional[str] = None,
    max_depth: Optional[int] = None,
    path: Optional[str] = None,
    paths: Optional[List[str]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """High-level repository inspection."""
    op = (operation or "overview").lower()
    wd = Path(workdir) if workdir else None

    if op == "summary" and paths:
        from src.tools.repo_read_tools import multi_file_summary

        return multi_file_summary(paths=paths, workdir=wd)  # type: ignore[arg-type]
    elif op == "structure":
        from src.tools.repo_read_tools import summarize_structure

        return summarize_structure(path=path or ".", workdir=wd)  # type: ignore[arg-type]
    elif op == "fingerprint":
        from src.tools.repo_read_tools import fingerprint_tech_stack

        return fingerprint_tech_stack(workdir=wd)  # type: ignore[arg-type]
    elif op == "analyze":
        from src.tools.repo_read_tools import analyze_repository

        return analyze_repository(workdir=wd)  # type: ignore[arg-type]
    else:
        from src.tools.repo_read_tools import repo_overview

        return repo_overview(workdir=wd, max_depth=max_depth or 3)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. files_write
# ---------------------------------------------------------------------------


@tool(
    name="files_write",
    description="Write file contents atomically or create directories.",
    side_effects=["write"],
    permission_kind=PermissionKind.WRITE_FILE,
    tags=["coding"],
)
def files_write(
    path: str,
    content: Optional[str] = None,
    operation: str = "write",
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Write file or create directory."""
    op = (operation or "write").lower()
    wd = Path(workdir) if workdir else None

    if op == "create_dir":
        from src.tools.file_tools import create_directory

        return create_directory(path=path, workdir=wd)  # type: ignore[arg-type]
    else:
        from src.tools.file_tools import write_file

        return write_file(path=path, content=content or "", workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. files_edit
# ---------------------------------------------------------------------------


@tool(
    name="files_edit",
    description="Edit existing file using exact text replacement or line ranges.",
    side_effects=["write"],
    permission_kind=PermissionKind.WRITE_FILE,
    tags=["coding"],
)
def files_edit(
    path: str,
    operation: str = "exact",
    old_str: Optional[str] = None,
    new_str: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    new_content: Optional[str] = None,
    edits: Optional[List[Dict[str, str]]] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Edit existing file."""
    op = (operation or "exact").lower()
    wd = Path(workdir) if workdir else None

    if op == "range" and start_line is not None and end_line is not None:
        from src.tools.file_tools import edit_by_line_range

        return edit_by_line_range(
            path=path,
            start_line=start_line,
            end_line=end_line,
            new_content=new_content or "",
            workdir=wd,  # type: ignore[arg-type]
        )
    elif op == "multi" and edits:
        from src.tools.file_tools import multiedit

        return multiedit(path=path, edits=edits, workdir=wd)  # type: ignore[arg-type]
    else:
        from src.tools.file_tools import edit_file_atomic

        return edit_file_atomic(
            path=path,
            old_str=old_str or "",
            new_str=new_str or "",
            workdir=wd,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 9. files_move
# ---------------------------------------------------------------------------


@tool(
    name="files_move",
    description="Move or rename a file within the workspace.",
    side_effects=["write"],
    permission_kind=PermissionKind.WRITE_FILE,
    tags=["coding"],
)
def files_move(
    source: str,
    destination: str,
    overwrite: bool = False,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Move or rename file."""
    from src.tools.file_tools import rename_file

    wd = Path(workdir) if workdir else None
    return rename_file(source=source, destination=destination, workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 10. files_delete
# ---------------------------------------------------------------------------


@tool(
    name="files_delete",
    description="Delete a file within the workspace.",
    side_effects=["write"],
    permission_kind=PermissionKind.WRITE_FILE,
    tags=["coding"],
)
def files_delete(
    path: str,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Delete file with snapshot guard."""
    from src.tools.file_tools import delete_file

    wd = Path(workdir) if workdir else None
    return delete_file(path=path, workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 11. patch
# ---------------------------------------------------------------------------


@tool(
    name="patch",
    description="Generate or apply a unified diff patch.",
    side_effects=["write"],
    permission_kind=PermissionKind.WRITE_FILE,
    tags=["coding"],
)
def patch(
    patch: Optional[str] = None,
    path: Optional[str] = None,
    new_content: Optional[str] = None,
    operation: str = "apply",
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Generate or apply patch."""
    op = (operation or "apply").lower()
    wd = Path(workdir) if workdir else None

    if op == "preview" and path and new_content is not None:
        from src.tools.patch_tools import generate_patch

        return generate_patch(path=path, new_content=new_content, workdir=wd)  # type: ignore[arg-type]
    elif patch:
        from src.tools.patch_tools import apply_patch

        return apply_patch(patch=patch, workdir=wd)  # type: ignore[arg-type]
    return err("Invalid patch arguments: specify patch (for apply) or path + new_content (for preview).")


# ---------------------------------------------------------------------------
# 12. shell
# ---------------------------------------------------------------------------


@tool(
    name="shell",
    description="Execute shell commands inside the workspace sandbox.",
    side_effects=["execute"],
    permission_kind=PermissionKind.EXECUTE_BASH,
    tags=["coding", "debug"],
)
def shell(
    command: str,
    operation: str = "execute",
    timeout: int = 60,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Execute command in sandbox."""
    op = (operation or "execute").lower()
    wd = Path(workdir) if workdir else None

    if op == "readonly":
        from src.tools.file_tools import bash_readonly

        return bash_readonly(command=command, workdir=wd)  # type: ignore[arg-type]
    else:
        from src.tools.file_tools import bash

        return bash(command=command, workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 13. tasks_wait
# ---------------------------------------------------------------------------


@tool(
    name="tasks_wait",
    description="Check status or wait for a background task.",
    permission_kind=PermissionKind.NONE,
    tags=["coding"],
)
def tasks_wait(
    task_id: str,
    operation: str = "check",
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Check background task status."""
    from src.tools.project_tools import check_background_task

    return check_background_task(task_id=task_id)


# ---------------------------------------------------------------------------
# 14. verify
# ---------------------------------------------------------------------------


@tool(
    name="verify",
    description="Run tests, linter, or syntax checks to verify workspace state.",
    permission_kind=PermissionKind.EXECUTE_BASH,
    tags=["coding", "debug"],
)
def verify(
    operation: str = "test",
    path: Optional[str] = None,
    test_name: Optional[str] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run tests, linter, or syntax verification."""
    op = (operation or "test").lower()
    wd = Path(workdir) if workdir else None

    if op == "lint":
        from src.tools.verification_tools import run_linter

        return run_linter(path=path or ".", workdir=wd)  # type: ignore[arg-type]
    elif op == "syntax":
        from src.tools.verification_tools import syntax_check

        return syntax_check(path=path or ".", workdir=wd)  # type: ignore[arg-type]
    else:
        from src.tools.verification_tools import run_tests

        return run_tests(path=path or "tests", test_name=test_name, workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 15. format
# ---------------------------------------------------------------------------


@tool(
    name="format",
    description="Format file or preview formatting diffs.",
    side_effects=["write"],
    permission_kind=PermissionKind.WRITE_FILE,
    tags=["coding"],
)
def format_tool(
    file_path: str,
    operation: str = "format",
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run code formatter on file."""
    from src.tools.formatter import format_file

    return format_file(file_path=file_path)


# ---------------------------------------------------------------------------
# 16. git
# ---------------------------------------------------------------------------


@tool(
    name="git",
    description="Inspect git status/diff or manage commits, stash, and restore.",
    permission_kind=PermissionKind.GIT_READ,
    tags=["coding"],
)
def git(
    operation: str = "status",
    message: Optional[str] = None,
    path: Optional[str] = None,
    max_count: int = 10,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Execute git operations."""
    op = (operation or "status").lower()
    wd = Path(workdir) if workdir else Path(".")

    if op == "log":
        from src.tools.git_tools import git_log

        return git_log(max_count=max_count, workdir=wd)
    elif op == "diff":
        from src.tools.git_tools import get_git_diff

        return get_git_diff(path=path, workdir=wd)
    elif op == "commit":
        from src.tools.git_tools import git_commit

        return git_commit(message=message or "Update", workdir=wd)
    elif op == "stash":
        from src.tools.git_tools import git_stash

        return git_stash(message=message or "", workdir=wd)
    elif op == "restore":
        from src.tools.git_tools import git_restore

        return git_restore(path=path or ".", workdir=wd)
    else:
        from src.tools.git_tools import git_status

        return git_status(workdir=wd)


# ---------------------------------------------------------------------------
# 17. lsp
# ---------------------------------------------------------------------------


@tool(
    name="lsp",
    description="Language server queries (diagnostics, definition, symbols, hover, rename).",
    permission_kind=PermissionKind.LSP_READ,
    tags=["coding", "debug"],
)
async def lsp(
    path: str,
    operation: str = "diagnostics",
    line: Optional[int] = None,
    col: Optional[int] = None,
    new_name: Optional[str] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Language server operations."""
    op = (operation or "diagnostics").lower()

    if op == "definition" and line is not None and col is not None:
        from src.tools.lsp_tools import lsp_definition

        return await lsp_definition(path=path, line=line, col=col, working_dir=workdir)
    elif op == "references" and line is not None and col is not None:
        from src.tools.lsp_tools import lsp_references

        return await lsp_references(path=path, line=line, col=col, working_dir=workdir)
    elif op == "symbols":
        from src.tools.lsp_tools import lsp_symbols

        return await lsp_symbols(path=path, working_dir=workdir)
    elif op == "hover" and line is not None and col is not None:
        from src.tools.lsp_tools import lsp_hover

        return await lsp_hover(path=path, line=line, col=col, working_dir=workdir)
    elif op == "rename" and line is not None and col is not None and new_name:
        from src.tools.lsp_tools import lsp_rename

        return await lsp_rename(path=path, line=line, col=col, new_name=new_name, working_dir=workdir)
    else:
        from src.tools.lsp_tools import lsp_diagnostics

        return await lsp_diagnostics(path=path, working_dir=workdir)


# ---------------------------------------------------------------------------
# 18. web_search
# ---------------------------------------------------------------------------


@tool(
    name="web_search",
    description="Search the web for technical documentation and answers.",
    permission_kind=PermissionKind.NETWORK,
    tags=["analysis"],
)
def web_search_tool(
    query: str,
    max_results: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Search web."""
    from src.tools.web_tools import web_search

    return web_search(query=query, max_results=max_results)


# ---------------------------------------------------------------------------
# 19. web_read
# ---------------------------------------------------------------------------


@tool(
    name="web_read",
    description="Fetch web page content with SSRF and redirect validation.",
    permission_kind=PermissionKind.NETWORK,
    tags=["analysis"],
)
def web_read_tool(
    url: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch web page."""
    from src.tools.web_tools import read_web_page

    return read_web_page(url=url)


# ---------------------------------------------------------------------------
# 20. memory
# ---------------------------------------------------------------------------


@tool(
    name="memory",
    description="Search or store long-term project memories and lessons.",
    permission_kind=PermissionKind.MEMORY,
    tags=["coding", "analysis"],
)
def memory(
    operation: str = "search",
    query: Optional[str] = None,
    content: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Memory operations."""
    op = (operation or "search").lower()

    if op == "save" and content:
        from src.tools.memory_tools import save_memory

        return save_memory(content=content)
    elif op == "mistake" and content:
        from src.tools.memory_tools import record_mistake

        return record_mistake(content=content)
    else:
        from src.tools.memory_tools import memory_search

        return memory_search(query=query or "")


# ---------------------------------------------------------------------------
# 21. state
# ---------------------------------------------------------------------------


@tool(
    name="state",
    description="Manage checkpoints, list snapshots, inspect diff, or revert changes.",
    permission_kind=PermissionKind.WRITE_FILE,
    tags=["coding"],
)
def state(
    operation: str = "checkpoint",
    name: Optional[str] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """State snapshot and checkpoint operations."""
    op = (operation or "checkpoint").lower()
    wd = Path(workdir) if workdir else None

    if op == "list":
        from src.tools.state_tools import list_checkpoints

        return list_checkpoints(workdir=wd)  # type: ignore[arg-type]
    elif op == "diff":
        from src.tools.state_tools import diff_state

        return diff_state(workdir=wd)  # type: ignore[arg-type]
    elif op == "revert":
        from src.tools.state_tools import revert_last_tool

        return revert_last_tool(workdir=wd)  # type: ignore[arg-type]
    else:
        from src.tools.state_tools import create_state_checkpoint

        return create_state_checkpoint(name=name or "auto", workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 22. todo
# ---------------------------------------------------------------------------


@tool(
    name="todo",
    description="Manage session todo list and task milestones.",
    side_effects=["write"],
    permission_kind=PermissionKind.WRITE_FILE,
    tags=["coding", "planning"],
)
def todo(
    operation: str = "list",
    text: Optional[str] = None,
    task_id: Optional[str] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Manage task items."""
    from src.tools.todo_tools import manage_todo

    wd = Path(workdir) if workdir else None
    return manage_todo(action=operation, text=text, task_id=task_id, workdir=wd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 23. skills
# ---------------------------------------------------------------------------


@tool(
    name="skills",
    description="List available skills or load instructions for a skill.",
    permission_kind=PermissionKind.NONE,
    tags=["coding", "analysis"],
)
def skills(
    operation: str = "list",
    name: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """List or load skills."""
    op = (operation or "list").lower()

    if op == "load" and name:
        from src.tools.skill_tools import load_skill

        return load_skill(name=name)
    else:
        from src.tools.skill_tools import list_skills

        return list_skills()


# ---------------------------------------------------------------------------
# 24. user_interact
# ---------------------------------------------------------------------------


@tool(
    name="user_interact",
    description="Prompt user for guidance or send an informational progress notification.",
    permission_kind=PermissionKind.NONE,
    tags=["coding", "planning"],
)
def user_interact(
    message: str,
    operation: str = "ask",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Ask or notify user."""
    op = (operation or "ask").lower()

    if op == "notify":
        from src.tools.interaction_tools import send_user_message

        return send_user_message(message=message)
    else:
        from src.tools.interaction_tools import ask_user

        return ask_user(question=message)


# ---------------------------------------------------------------------------
# 25. delegate
# ---------------------------------------------------------------------------


@tool(
    name="delegate",
    description="Delegate a task to an autonomous sub-agent.",
    permission_kind=PermissionKind.DELEGATE,
    tags=["coding", "planning"],
)
def delegate(
    task: str,
    operation: str = "spawn",
    role: Optional[str] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    workdir: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Delegate subtasks."""
    op = (operation or "spawn").lower()

    if op == "parallel" and tasks:
        from src.tools.subagent_tools import delegate_tasks_parallel

        return delegate_tasks_parallel(tasks=tasks, workdir=workdir)
    else:
        from src.tools.subagent_tools import delegate_task

        return delegate_task(task=task, role=role or "coder", workdir=workdir)


# ---------------------------------------------------------------------------
# 26. plan
# ---------------------------------------------------------------------------


@tool(
    name="plan",
    description="Manage planning mode or submit a plan for user review.",
    permission_kind=PermissionKind.PLAN,
    tags=["planning"],
)
def plan(
    operation: str = "enter",
    plan: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Planning operations."""
    op = (operation or "enter").lower()

    if op == "exit":
        from src.tools.plan_mode_tools import exit_plan_mode

        return exit_plan_mode()
    elif op == "submit" and plan:
        from src.tools.plan_mode_tools import submit_plan_for_review

        return submit_plan_for_review(plan=plan)
    else:
        from src.tools.plan_mode_tools import enter_plan_mode

        return enter_plan_mode()


# ---------------------------------------------------------------------------
# 27. batch
# ---------------------------------------------------------------------------


@tool(
    name="batch",
    description="Execute a batch of read-only tool calls.",
    permission_kind=PermissionKind.NONE,
    tags=["coding"],
)
def batch_tool(
    calls: List[Dict[str, Any]],
    **kwargs: Any,
) -> Dict[str, Any]:
    """Execute batch of tool calls."""
    from src.tools.batch_tools import batch

    return batch(calls=calls)
