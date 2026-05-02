"""registry_builder.py — Phase B: default tool registry factory.

``example_registry()`` builds the full set of built-in tools by delegating to
``src.tools._registry.build_registry`` (auto-discovery of ``@tool``-decorated
functions) with a manual fallback that registers each tool package explicitly.

Extracted from ``orchestrator.py`` so that adding a new tool package no longer
requires editing the 4000-line orchestrator.
"""

from __future__ import annotations

import logging

from src.core.orchestration.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


def example_registry() -> ToolRegistry:
    """Build the default tool registry.

    Delegates to ``src.tools.build_registry()`` for auto-discovery of all
    ``@tool``-decorated functions, then wraps the results in the local
    ``ToolRegistry`` so existing code that accesses ``.tools`` still works.
    """
    try:
        from src.tools._registry import build_registry as _build

        _new_reg = _build(include_echo=True)
        reg = ToolRegistry()
        for name in _new_reg.list():
            entry = _new_reg.get(name)
            if entry:
                reg.tools[name] = {
                    "fn": entry["fn"],
                    "side_effects": entry.get("side_effects", []),
                    "description": entry.get("description", ""),
                }
        # Ensure legacy edit_by_line_range comes from file_tools when available.
        # Some discovery paths may not include _edit_tools; force the file_tools
        # implementation into the registry so tests and callers see a single
        # canonical registration originating from file_tools.
        try:
            from src.tools import file_tools  # type: ignore[import]

            if hasattr(file_tools, "edit_by_line_range"):
                reg.tools["edit_by_line_range"] = {
                    "fn": file_tools.edit_by_line_range,
                    "side_effects": ["write"],
                    "description": "edit_by_line_range(path, start_line, end_line, new_content) -> Replace lines in file",
                }
        except AttributeError as exc:
            logger.debug("registry_builder: edit_by_line_range override skipped: %s", exc)
        return reg
    except Exception as exc:
        logger.warning("registry_builder: auto-discovery failed (%s); falling back to manual registration", exc)

    from src.tools import file_tools  # noqa: PLC0415

    reg = ToolRegistry()

    # Repo Intelligence Tools
    from src.tools import repo_tools
    from src.tools import repo_analysis_tools

    reg.register(
        "initialize_repo_intelligence",
        repo_tools.initialize_repo_intelligence,
        description="initialize_repo_intelligence() -> Indexes the repository to enable code search and symbol finding.",
    )
    reg.register(
        "analyze_repository",
        repo_analysis_tools.analyze_repository,
        description="analyze_repository() -> Analyzes the repository and creates a repo_memory.json file with summaries and dependencies.",
    )

    # Simple echo tool used by unit tests
    def _echo(text: str, **kwargs):
        return {"status": "ok", "output": text}

    try:
        reg.register(
            "echo", _echo, description="echo(text) -> Return the provided text"
        )
    except Exception:
        pass
    reg.register(
        "search_code",
        repo_tools.search_code,
        description="search_code(query) -> Performs semantic search for code snippets.",
    )
    reg.register(
        "find_symbol",
        repo_tools.find_symbol,
        description="find_symbol(name) -> Finds a class or function by its exact name.",
    )
    # add find_references if available
    try:
        reg.register(
            "find_references",
            repo_tools.find_references,
            description="find_references(name) -> Find references to a symbol across the repo.",
        )
    except Exception:
        pass

    # Core file operations
    reg.register(
        "list_files",
        file_tools.list_files,
        description="list_files(path) -> List files in a directory",
    )
    reg.register(
        "read_file",
        file_tools.read_file,
        description="read_file(path) -> Read file contents",
    )
    # alias: fs.read
    try:
        reg.register("fs.read", file_tools.read_file, description="alias for read_file")
    except Exception:
        pass
    # read_file_chunk for incremental reading
    reg.register(
        "read_file_chunk",
        file_tools.read_file_chunk,
        description="read_file_chunk(path, offset, limit) -> Read file contents with offset and limit",
    )
    reg.register(
        "write_file",
        file_tools.write_file,
        side_effects=["write"],
        description="write_file(path, content) -> Write content to a file",
    )
    # alias: fs.write
    try:
        reg.register(
            "fs.write",
            file_tools.write_file,
            side_effects=["write"],
            description="alias for write_file",
        )
    except Exception:
        pass
    reg.register(
        "edit_file",
        file_tools.edit_file,
        side_effects=["write"],
        description="edit_file(path, patch) -> Edit a file using a unified diff patch",
    )
    reg.register(
        "edit_file_atomic",
        file_tools.edit_file_atomic,
        side_effects=["write"],
        description=(
            "edit_file_atomic(path, old_string, new_string) -> "
            "Replace old_string (must appear exactly once) with new_string. "
            "Preferred for surgical edits: no line-number drift, fails loudly if ambiguous."
        ),
    )
    # F6: edit_by_line_range — precise multi-line replacement without full-file rewrite
    reg.register(
        "edit_by_line_range",
        file_tools.edit_by_line_range,
        side_effects=["write"],
        description=(
            "edit_by_line_range(path, start_line, end_line, new_content) -> "
            "Replace lines [start_line, end_line] (1-indexed, inclusive) with new_content."
        ),
    )
    reg.register(
        "delete_file",
        file_tools.delete_file,
        side_effects=["write"],
        description="delete_file(path) -> Delete a file or directory from the workspace",
    )
    reg.register(
        "rename_file",
        file_tools.rename_file,
        side_effects=["write"],
        description="rename_file(src_path, dst_path) -> Rename or move a file within the workspace",
    )
    # alias: fs.list
    try:
        reg.register(
            "fs.list", file_tools.list_files, description="alias for list_files"
        )
    except Exception:
        pass

    # MVP Tools: bash and glob
    reg.register(
        "bash",
        file_tools.bash,
        side_effects=["execute"],
        description=(
            "bash(command, timeout_secs=60, run_in_background=False) -> "
            "Execute a safe, allowlisted shell command (read-only queries, git, test runners, compilers). "
            "Shell operators (|, &&, >) and destructive commands are blocked. "
            "Returns stdout, stderr, returncode, interrupted, no_output_expected; "
            "stdout_truncated/stderr_truncated when output was cut. "
            "Prefer bash_readonly for pure read-only inspection."
        ),
    )
    reg.register(
        "bash_readonly",
        file_tools.bash_readonly,
        side_effects=["execute"],
        description=(
            "bash_readonly(command, timeout_secs=60) -> "
            "Execute a read-only shell command (ls, cat, grep, git log, etc.). "
            "Sandboxed with network disabled. Only tier-1 SAFE_COMMANDS allowed — "
            "no test runners, no compilers, no package managers. "
            "Prefer this over bash() for all inspection tasks."
        ),
    )
    reg.register(
        "check_background_task",
        file_tools.check_background_task,
        description=(
            "check_background_task(task_id) -> "
            "Poll whether a background process started with bash(run_in_background=True) is still running. "
            "task_id is the background_task_id (PID) returned by that call. "
            "Returns {running, pid, exit_code}."
        ),
    )
    reg.register(
        "glob",
        file_tools.glob,
        description="glob(pattern) -> Find files matching a glob pattern, sorted newest-first by modification time.",
    )

    # ToolOptimization Phase 1: Pattern Search & Git
    try:
        from src.tools import system_tools

        reg.register(
            "grep",
            system_tools.grep,
            description=(
                "grep(pattern, path, include='', context=0) -> "
                "Regex search in files. "
                "pattern: regex to match. "
                "path: file or directory to search (default: working dir). "
                "include: glob filter e.g. '*.py', '*.ts'. "
                "context: lines of surrounding context to include. "
                "Use for finding exact strings, function names, imports, TODOs, or any "
                "pattern across files. Prefer over search_code when you know the exact "
                "text. Returns {matches: [{file_path, line_number, content}]}."
            ),
        )
        reg.register(
            "summarize_structure",
            system_tools.summarize_structure,
            description="summarize_structure() -> Get workspace summary (files, dirs, sizes)",
        )
    except Exception:
        pass

    # ToolOptimization Phase 6: State Checkpoints
    try:
        from src.tools import state_tools as st

        reg.register(
            "create_state_checkpoint",
            st.create_state_checkpoint,
            description="create_state_checkpoint(task, history, files, summary) -> Save current state",
        )
        reg.register(
            "list_checkpoints",
            st.list_checkpoints,
            description="list_checkpoints() -> List available state checkpoints",
        )
        reg.register(
            "restore_state_checkpoint",
            st.restore_state_checkpoint,
            description="restore_state_checkpoint(checkpoint_id) -> Restore a previous checkpoint",
        )
        reg.register(
            "diff_state",
            st.diff_state,
            description="diff_state(id1, id2) -> Compare two checkpoints",
        )
    except Exception:
        pass

    # ToolOptimization Phase 8: Batched Tools
    try:
        from src.tools import state_tools as st

        reg.register(
            "batched_file_read",
            st.batched_file_read,
            description="batched_file_read(paths) -> Read multiple files efficiently",
        )
        reg.register(
            "multi_file_summary",
            st.multi_file_summary,
            description="multi_file_summary(paths) -> Get info on multiple files without reading",
        )
    except Exception:
        pass

    # Verification tools (added)
    try:
        from src.tools import verification_tools

        reg.register(
            "run_tests",
            verification_tools.run_tests,
            description="run_tests(workdir) -> Run pytest in the working directory",
        )
        reg.register(
            "run_linter",
            verification_tools.run_linter,
            description="run_linter(workdir) -> Run ruff in the working directory",
        )
        reg.register(
            "syntax_check",
            verification_tools.syntax_check,
            description="syntax_check(workdir) -> Quick py_compile across repo",
        )
        reg.register(
            "run_js_tests",
            verification_tools.run_js_tests,
            description="run_js_tests(workdir) -> Run JS/TypeScript tests via jest/vitest/mocha",
        )
        reg.register(
            "run_ts_check",
            verification_tools.run_ts_check,
            description="run_ts_check(workdir) -> TypeScript type-check via tsc --noEmit",
        )
        reg.register(
            "run_eslint",
            verification_tools.run_eslint,
            description="run_eslint(workdir, paths) -> Run ESLint on JS/TypeScript files",
        )
    except Exception:
        pass

    # Memory utilities
    try:
        from src.core.memory import memory_tools

        reg.register(
            "memory_search",
            memory_tools.memory_search,
            description="memory_search(query) -> Search TASK_STATE.md and execution trace for relevant entries",
        )
    except Exception:
        pass

    # Patch and role management tools
    try:
        from src.tools import patch_tools

        reg.register(
            "generate_patch",
            patch_tools.generate_patch,
            description="generate_patch(path, new_content) -> Produce unified diff patch",
        )
        reg.register(
            "apply_patch",
            patch_tools.apply_patch,
            side_effects=["write"],
            description="apply_patch(path, patch) -> Apply unified diff patch to a file",
        )
    except Exception:
        pass

    # O4: role_tools (set_role / get_role) intentionally NOT registered.
    # Allowing the LLM to change its own role at runtime via a tool call is dangerous:
    # an adversarial prompt could switch to "operational" mid-debug.
    # Role management belongs to AgentBrainManager, not the tool interface.

    # Subagent tools
    try:
        from src.tools import subagent_tools

        reg.register(
            "delegate_task",
            subagent_tools.delegate_task,
            description="delegate_task(role, subtask_description, working_dir) -> Spawn an isolated subagent (analyst/strategic/reviewer/operational/debugger) to complete a subtask and return a summary",
        )
        reg.register(
            "list_subagent_roles",
            subagent_tools.list_subagent_roles,
            description="list_subagent_roles() -> List available subagent roles",
        )
    except Exception:
        pass

    # Git tools (F19)
    try:
        from src.tools import git_tools

        reg.register(
            "git_status",
            git_tools.git_status,
            description="git_status(workdir) -> Show working-tree status (branch + modified/untracked files)",
        )
        reg.register(
            "git_log",
            git_tools.git_log,
            description="git_log(workdir, max_count=10) -> Show last N commits (hash + subject)",
        )
        reg.register(
            "git_diff",
            git_tools.git_diff,
            description="git_diff(workdir, staged=False, path=None) -> Show unified diff of working-tree or staged changes",
        )
        reg.register(
            "git_commit",
            git_tools.git_commit,
            side_effects=["write"],
            description="git_commit(message, workdir, add_all=True) -> Stage all changes and create a commit",
        )
        reg.register(
            "git_stash",
            git_tools.git_stash,
            side_effects=["write"],
            description="git_stash(workdir, message=None) -> Stash all local modifications",
        )
        reg.register(
            "git_restore",
            git_tools.git_restore,
            side_effects=["write"],
            description="git_restore(path, workdir, staged=False) -> Discard working-tree changes to a file",
        )
    except Exception:
        pass

    # TODO tracking tool
    try:
        from src.tools.todo_tools import manage_todo

        reg.register(
            "manage_todo",
            manage_todo,
            side_effects=["write"],
            description=(
                "manage_todo(action, workdir, steps, step_id, description) -> "
                "Manage the task TODO list. "
                "action='create': create TODO from steps list. "
                "action='check': mark step_id as done. "
                "action='update': update step_id description. "
                "action='read': return current TODO. "
                "action='clear': remove TODO."
            ),
        )
    except Exception:
        pass

    # Batch tool — parallel multi-tool execution
    try:
        from src.tools import batch_tools

        reg.register(
            "batch",
            batch_tools.batch,
            description=(
                "batch(calls) -> Execute multiple tool calls in parallel. "
                'calls is a list of {"tool": name, "input": {...}}. '
                "Maximum 10 calls. Returns all results in order. "
                "Use for independent read operations that can run concurrently."
            ),
        )
    except Exception:
        pass

    # Multiedit tool — atomic multi-replacement on a single file
    try:
        reg.register(
            "multiedit",
            file_tools.multiedit,
            side_effects=["write"],
            description=(
                "multiedit(path, edits) -> Apply multiple old_string→new_string "
                "replacements to a single file atomically. All edits validated "
                "in memory before writing. Use instead of repeated edit_file_atomic calls."
            ),
        )
    except Exception:
        pass

    # Skill tools — LLM-callable skill loader
    try:
        from src.tools import skill_tools

        reg.register(
            "load_skill",
            skill_tools.load_skill,
            description="load_skill(name) -> Load a named skill/prompt template from the skills directory.",
        )
        reg.register(
            "list_skills",
            skill_tools.list_skills,
            description="list_skills() -> List available skill names in the skills directory.",
        )
    except Exception:
        pass

    # Web tools
    try:
        from src.tools import web_tools

        reg.register(
            "web_search",
            web_tools.web_search,
            description=(
                "web_search(query, max_results=5) -> Search the web for documentation, "
                "error messages, or package information. Returns titles, URLs, and snippets."
            ),
        )
        reg.register(
            "read_web_page",
            web_tools.read_web_page,
            description=(
                "read_web_page(url, format='markdown') -> Fetch and return text content of a "
                "web page (up to 100 000 chars). format='markdown' or 'text'. "
                "HTTP URLs are upgraded to HTTPS automatically."
            ),
        )
    except Exception:
        pass

    # Interaction tools
    try:
        from src.tools import interaction_tools

        reg.register(
            "ask_user",
            interaction_tools.ask_user,
            description=(
                "ask_user(question, choices=None) -> Pause and ask the user a clarifying "
                "question. Pass choices=[...] for a multiple-choice prompt. Blocks until "
                "the user responds (timeout 5 min)."
            ),
        )
        reg.register(
            "submit_plan_for_review",
            interaction_tools.submit_plan_for_review,
            description=(
                "submit_plan_for_review(plan_summary, plan_steps, risk_level='medium') -> "
                "Submit the current plan for user approval before execution."
            ),
        )
    except Exception:
        pass

    return reg
