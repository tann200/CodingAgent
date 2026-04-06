"""Orchestrator that manages the agent runner, tool registry, preflight checks and execution.

This module provides:
- ToolRegistry: simple in-memory mapping of tools
- Orchestrator: wires ProviderManager events, publishes telemetry, performs non-blocking model checks
- Example builtin tools and example_registry helper
"""

from __future__ import annotations

import logging
import time
import uuid
import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# pydantic ValidationError: provide a safe alias even if pydantic is not installed
try:
    import importlib

    _pyd = importlib.import_module("pydantic")
    PydValidationError = getattr(_pyd, "ValidationError")
except Exception:

    class PydValidationError(Exception):
        """Fallback ValidationError when pydantic is not installed."""

        pass


# Keep the name `ValidationError` for backward compatibility across the module
ValidationError = PydValidationError

# Tools that require the target file to have been read in the current session before writing
WRITE_TOOLS_REQUIRING_READ = {
    "edit_file",
    "edit_file_atomic",
    "write_file",
    "edit_by_line_range",
    "apply_patch",
    # Destructive tools — also subject to the _affected_files scope guard
    "delete_file",
    "rename_file",
    "ast_rename",
    "manage_todo",  # SEC-2: sync with MODIFYING_TOOLS in loop_guards
}

# Tools that always require explicit user approval before execution
PERMISSION_REQUIRED_TOOLS = {
    "delete_file",
    "run_bash",
}

# Import tool_contracts defensively; many CI/test environments may omit heavy deps
# Using try/except to handle both environments gracefully
try:
    from src.core.orchestration.tool_contracts import get_tool_contract
    from src.core.orchestration.tool_contracts import ToolContract  # type: ignore[attr-defined]
except ImportError:
    # Fallback no-op implementations for environments without pydantic
    def get_tool_contract(name: str) -> Any:
        return None

    class ToolContract:  # type: ignore[no-redef]
        @staticmethod
        def model_validate(obj: Any) -> Any:
            return obj


from src.core.inference.llm_manager import (  # noqa: E402
    get_provider_manager,
    _ensure_provider_manager_initialized_sync,
)
from src.core.orchestration.event_bus import EventBus, new_correlation_id  # noqa: E402
from src.core.orchestration.message_manager import MessageManager  # noqa: E402
from src.core.logger import logger as guilogger  # noqa: E402
from src.tools import file_tools  # noqa: E402
from src.tools.registry import register_tool  # noqa: E402

logger = logging.getLogger(__name__)


def _write_permission_audit(
    working_dir: "Any",
    tool_name: str,
    args: dict,
    decision: str,
    reason: str = "",
) -> None:
    """PERM-W5: Append a permission audit entry to .agent/permission_audit.jsonl.

    Each line is a JSON object with fields: timestamp, tool, decision, reason.
    The file uses append-only JSONL so it never grows unboundedly per-process
    (a new process or session resets nothing — the file accumulates across runs).
    Args are not logged to avoid leaking sensitive values.
    """
    import json as _json
    import datetime as _dt
    from pathlib import Path as _Path

    try:
        wd = _Path(str(working_dir)) if working_dir else _Path.cwd()
        audit_path = wd / ".agent" / "permission_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        entry = _json.dumps(
            {
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "tool": tool_name,
                "decision": decision,
                "reason": reason,
            }
        )
        with audit_path.open("a", encoding="utf-8") as _f:
            _f.write(entry + "\n")
    except Exception:
        pass  # audit failures must never block tool execution


def _is_git_repo(path: str) -> bool:
    """Check if path is inside a git repository."""
    try:
        import subprocess as _sp

        result = _sp.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=path,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except Exception:
        return False


def _get_git_diff_for_files(workdir: str, files: List[str]) -> str:
    """Get git diff stat for specific files only."""
    if not files:
        return ""
    try:
        import subprocess as _sp

        result = _sp.run(
            ["git", "diff", "--stat", "HEAD", "--"] + files,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=workdir,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _generate_work_summary(
    final_state: Optional[Dict[str, Any]], history: List[Dict[str, Any]]
) -> str:
    """Generate a summary of work done based on final state and history."""
    if not final_state:
        return ""

    task = final_state.get("task", final_state.get("original_task", ""))
    rounds = final_state.get("rounds", 0)
    current_plan = final_state.get("current_plan") or []
    current_step = final_state.get("current_step", 0)
    verified_reads = final_state.get("verified_reads") or []

    tool_counts: Dict[str, int] = {}
    tool_errors: List[str] = []
    for entry in history:
        if entry.get("role") == "tool" and entry.get("tool"):
            tool_name = entry.get("tool", "unknown")
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            # Collect tool-level errors to surface in summary
            result = entry.get("result") or {}
            if isinstance(result, dict):
                err = result.get("error") or result.get("message")
                if err and result.get("status") == "error":
                    tool_errors.append(f"{tool_name}: {err}")

    # Determine overall outcome from last_result
    last_result = final_state.get("last_result") or {}
    last_ok = last_result.get("ok") or last_result.get("status") == "ok"
    no_plan_fail = int(final_state.get("no_plan_fail_count") or 0)
    task_failed = (not last_ok and last_result) or no_plan_fail >= 3

    completed_steps = []
    pending_steps = []
    if current_plan:
        for i, step in enumerate(current_plan):
            desc = step.get("description", f"Step {i + 1}")
            if step.get("completed"):
                completed_steps.append(desc)
            elif i >= current_step:
                pending_steps.append(desc)

    lines = ["", "---", "**Work Summary**", ""]
    lines.append(f"- Task: {task}")
    lines.append(f"- Rounds: {rounds}")

    # Outcome indicator
    if task_failed:
        lines.append("- Outcome: ✗ Failed")
    else:
        lines.append("- Outcome: ✓ Completed")

    if tool_counts:
        tools_str = ", ".join(
            f"{count}× {name}" for name, count in sorted(tool_counts.items())
        )
        lines.append(f"- Tools used: {tools_str}")

    if verified_reads:
        lines.append(f"- Files inspected: {len(verified_reads)}")

    if completed_steps:
        lines.append(f"- Steps completed: {len(completed_steps)}/{len(current_plan)}")
        for step in completed_steps:
            lines.append(f"  - {step}")

    if pending_steps:
        lines.append(f"- Pending steps: {len(pending_steps)}")

    # Surface tool errors so the user can see what went wrong
    if tool_errors:
        lines.append("- Errors:")
        for err in tool_errors[-3:]:  # cap at last 3 to keep summary readable
            lines.append(f"  - ✗ {err}")

    # Only show git diff if: git is available AND files were modified during this session
    working_dir = final_state.get("working_dir", ".")
    if _is_git_repo(working_dir):
        # Get files modified during this session
        modified_files = final_state.get("_session_modified_files") or []
        if modified_files:
            # Filter to files within working_dir and get relative paths
            try:
                workdir_path = Path(working_dir).resolve()
                relative_files = []
                for f in modified_files:
                    try:
                        fpath = Path(f).resolve()
                        if str(fpath).startswith(str(workdir_path)):
                            relative_files.append(str(fpath.relative_to(workdir_path)))
                    except Exception:
                        pass

                if relative_files:
                    # Get unified diff for side-by-side formatting
                    import subprocess as _sp

                    diff_result = _sp.run(
                        [
                            "git",
                            "diff",
                            "--",
                        ]
                        + relative_files,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=working_dir,
                    )
                    if diff_result.returncode == 0 and diff_result.stdout.strip():
                        unified_diff = diff_result.stdout.strip()
                        side_by_side = _format_side_by_side_diff(unified_diff)

                        lines.append("")
                        lines.append("**📋 Changes Made:**")
                        lines.append("")
                        lines.append("```diff")
                        lines.append(side_by_side)
                        lines.append("```")
            except Exception:
                pass

    return "\n".join(lines)


# Tool result formatting — moved to tool_result_formatter.py (single responsibility).
# Re-exported here for backward compatibility with existing imports.
from src.core.orchestration.tool_result_formatter import (  # noqa: E402
    TOOL_RESULT_FORMATTERS,
    format_tool_result as _format_tool_result,
    _format_side_by_side_diff,
    _format_list_files_result,
    _format_read_file_result,
    _format_grep_result,
    _format_search_result,
    _format_symbol_result,
    _format_change_summary,
)

# ── TUI-03/TUI-04: approval gate registries — moved to approval_gate.py ──────
# Re-exported here for backward compatibility (file_tools.py imports
# register_bash_gate and _bash_denied from this module).
import threading as _threading  # noqa: E402 (used by ToolRegistry below)
from src.core.orchestration.approval_gate import (  # noqa: E402
    _pending_bash,
    _bash_denied,
    _pending_tool,
    _tool_denied,
    register_bash_gate,
    resolve_bash_gate,
    register_tool_gate,
    resolve_tool_gate,
)


class ToolRegistry:
    def __init__(self) -> None:
        # name -> metadata dict (fn, side_effects, description)
        self.tools: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        side_effects: Optional[List[str]] = None,
        description: str = "",
    ) -> None:
        self.tools[name] = {
            "fn": fn,
            "side_effects": side_effects or [],
            "description": description,
        }
        # also register in global registry for other consumers
        try:
            register_tool(
                name, fn, description=description, side_effects=bool(side_effects)
            )
        except Exception:
            pass

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self.tools.get(name)

    def list(self) -> List[str]:
        return list(self.tools.keys())

    def filter_by_names(self, names: List[str]) -> "ToolRegistry":
        """Return a new ToolRegistry containing only tools whose names are in *names*."""
        filtered = ToolRegistry()
        for name in names:
            meta = self.tools.get(name)
            if meta:
                filtered.tools[name] = meta
        return filtered

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        """Convert registered tools to OpenAI function-calling format.

        MC-1 fix: Enable native function-calling API support. Tools are converted
        to the OpenAI /v1/chat/completions 'tools' parameter format with name,
        description, and inferred parameters from function signatures.
        """
        import inspect
        import re

        functions = []
        for name, meta in self.tools.items():
            desc = meta.get("description", "")
            fn = meta.get("fn")
            if not fn:
                continue

            # Parse parameters from docstring/signature
            params = {"type": "object", "properties": {}}
            required = []

            # Try to get signature
            try:
                sig = inspect.signature(fn)
                for pname, param in sig.parameters.items():
                    if pname in ("kwargs", "self", "cls"):
                        continue
                    ptype = "string"
                    if param.annotation != inspect.Parameter.empty:
                        ann = str(param.annotation).lower()
                        if "int" in ann:
                            ptype = "integer"
                        elif "float" in ann or "double" in ann:
                            ptype = "number"
                        elif "bool" in ann:
                            ptype = "boolean"
                        elif "list" in ann or "array" in ann:
                            ptype = "array"
                    params["properties"][pname] = {"type": ptype}
                    required.append(pname)
            except Exception:
                pass

            # Try to extract params from description
            desc_params = re.findall(r"(\w+)\s*:\s*(\w+)", desc)
            for pname, ptype in desc_params:
                if pname not in params["properties"]:
                    t = "string"
                    if ptype.lower() in ("int", "integer"):
                        t = "integer"
                    elif ptype.lower() in ("float", "number"):
                        t = "number"
                    elif ptype.lower() in ("bool", "boolean"):
                        t = "boolean"
                    elif ptype.lower() in ("list", "array"):
                        t = "array"
                    params["properties"][pname] = {"type": t}
                    if pname not in required:
                        required.append(pname)

            func_def = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": params,
                },
            }
            if required:
                func_def["function"]["parameters"]["required"] = required

            functions.append(func_def)

        return functions


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
        return reg
    except Exception:
        pass  # Fall through to manual registration below

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
        file_tools.list_dir,
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
        reg.register("fs.list", file_tools.list_dir, description="alias for list_files")
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


class Orchestrator:
    def __init__(
        self,
        adapter: Any = None,
        tool_registry: Optional[ToolRegistry] = None,
        working_dir: Optional[str | Path] = None,
        allow_external_working_dir: bool = False,
        message_max_tokens: Optional[int] = 4000,
        deterministic: bool = False,
        seed: Optional[int] = None,
        event_bus: Optional["EventBus"] = None,
        dry_run: bool = False,
    ):
        self._dry_run = dry_run
        self._adapter = adapter
        self._provider_name = ""  # set during provider resolution
        self.tool_registry = tool_registry if tool_registry else example_registry()
        # TASK-09: Filter tool registry using the active permission context (set by
        # --allowed-tools / --deny-tool / --deny-prefix CLI flags in main.py).
        try:
            from src.tools.permission_context import _ACTIVE_CONTEXT as _pctx

            if _pctx is not None and not _pctx.is_empty():
                _filtered = _pctx.filter_registry(self.tool_registry)
                if isinstance(_filtered, ToolRegistry):
                    self.tool_registry = _filtered
        except Exception:
            pass
        # TUI-01: accept an externally-created EventBus (e.g. from AgentBridge)
        # so that the bridge and the orchestrator share the same bus instance.
        self.event_bus = event_bus if event_bus is not None else EventBus()
        # Wire the MessageManager with compaction support so dropped messages
        # are summarised inline rather than silently discarded.
        self.msg_mgr = MessageManager(
            max_tokens=message_max_tokens,
            event_bus=self.event_bus,
            compact_callback=self._compact_messages,
        )

        self._session_read_files: set = set()
        self._session_modified_files: set = set()
        self._max_files_per_task = 10
        # F17: In-memory usage counter — flushed once per run_agent_once() instead of per tool call
        self._usage_buffer: dict = {}
        self.deterministic = bool(deterministic)
        self.seed = seed

        # HR-3 fix: create a single reusable ThreadPoolExecutor for tool timeouts.
        # Previously a new executor was created (and torn down) inside every execute_tool
        # call, adding ~5 ms overhead per tool invocation from thread-pool churn.
        import concurrent.futures as _cf_init

        self._tool_executor = _cf_init.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="tool_timeout"
        )

        # MED-5 fix: create a single reusable ThreadPoolExecutor for graph execution
        # instead of creating one per run_agent_once() call.  Shut down in close().
        self._graph_executor = _cf_init.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="graph_exec"
        )

        # Default working directory logic:
        # - If cwd is inside the project root (CodingAgent repo), use project_root/output (for testing)
        # - Otherwise, use the cwd where the app is started
        repo_root = Path(__file__).parents[3]
        default_out = repo_root / "output"
        try:
            cwd = Path.cwd().resolve()
            repo_root_resolved = repo_root.resolve()
            # Check if cwd is inside the project repo
            if str(cwd).startswith(str(repo_root_resolved)) or str(
                repo_root_resolved
            ) in str(cwd):
                default_wd = default_out
            else:
                default_wd = cwd
        except Exception:
            default_wd = default_out
        self.working_dir = Path(working_dir) if working_dir else default_wd
        self._allow_external = bool(allow_external_working_dir)
        self._ensure_working_dir()

        # Initialize RollbackManager for automated rollback on failure
        from src.core.orchestration.rollback_manager import RollbackManager

        self.rollback_manager = RollbackManager(str(self.working_dir))
        self._current_snapshot_id: Optional[str] = None
        # Step-level transaction snapshot for multi-file atomicity
        self._step_snapshot_id: Optional[str] = None

        # Initialize FileLockManager for PRSW (Parallel Reads, Sequential Writes)
        from src.core.orchestration.file_lock_manager import FileLockManager

        self.file_lock_manager = FileLockManager(
            workdir=str(self.working_dir),
            cancel_event=getattr(self, "cancel_event", None),  # type: ignore[arg-type]
        )

        # S4-A: Initialize GitSnapshotManager for workspace snapshots.
        # Snapshots are stored in a shadow git repo; tree hashes are appended
        # to AgentState.snapshots once per perception pass.
        from src.core.orchestration.snapshot_manager import GitSnapshotManager

        _project_id = self.working_dir.name or "default"
        self.snapshot_manager = GitSnapshotManager(
            workspace=self.working_dir,
            project_id=_project_id,
            enabled=True,
        )

        # Initialize SessionStore for tool call and plan persistence
        from src.core.memory.session_store import SessionStore

        self.session_store = SessionStore(str(self.working_dir))

        # Initialize SessionLifecycleManager for graceful shutdown and snapshots
        from src.core.orchestration.session_lifecycle import (
            get_session_lifecycle_manager,
        )

        self.lifecycle_manager = get_session_lifecycle_manager(str(self.working_dir))

        # Register lifecycle shutdown hook for cleanup
        def _lifecycle_cleanup_hook(session_id: str) -> None:
            try:
                # SessionStore auto-commits, but we can log the cleanup
                guilogger.debug(f"Lifecycle cleanup for session: {session_id}")
            except Exception as e:
                guilogger.warning(f"Lifecycle cleanup hook failed: {e}")

        self.lifecycle_manager.on_shutdown(
            "session_store_flush", _lifecycle_cleanup_hook
        )

        # Subscribe to task completion events for automatic snapshot.
        # MED-11 fix: snapshot whenever ANY task.completed event arrives for this
        # orchestrator — the session_id comparison against self._current_task_id was
        # racy (start_new_task() could update _current_task_id before the handler
        # fired, causing the snapshot to be silently skipped).  Snapshotting is
        # idempotent and inexpensive, so we always create one on task completion.
        def _on_task_complete(payload: Any) -> None:
            self._create_session_snapshot()

        self.event_bus.subscribe("task.completed", _on_task_complete)

        # Subscribe to task failure for snapshot before cleanup
        def _on_task_failed(payload: Any) -> None:
            if payload.get("session_id") == self._current_task_id:
                self._create_session_snapshot()

        self.event_bus.subscribe("task.failed", _on_task_failed)

        self._current_task_id: Optional[str] = None

        pm = None
        try:
            pm = get_provider_manager()
            if pm:
                _ensure_provider_manager_initialized_sync()
                if getattr(pm, "_event_bus", None) is None:
                    pm.set_event_bus(self.event_bus)
                else:
                    self.event_bus = getattr(pm, "_event_bus")

                # Pick default adapter if none provided
                if self._adapter is None:
                    providers = pm.list_providers()
                    guilogger.info(
                        f"Orchestrator init: available providers: {providers}"
                    )
                    if providers:
                        name = "lm_studio" if "lm_studio" in providers else providers[0]
                        self._adapter = pm.get_provider(name)
                        guilogger.info(
                            f"Orchestrator init: picked adapter: {name}, adapter: {self._adapter}"
                        )
        except Exception:
            pass

        try:
            payload = {"time": time.time(), "working_dir": str(self.working_dir)}
            try:
                guilogger.info("Orchestrator: publishing startup to self.event_bus")
                self.event_bus.publish("orchestrator.startup", payload)
            except Exception:
                pass

            try:
                pm_bus = getattr(pm, "_event_bus", None)
                if pm_bus and pm_bus is not self.event_bus:
                    guilogger.info("Orchestrator: publishing startup to pm_bus")
                    pm_bus.publish("orchestrator.startup", payload)
            except Exception:
                pass
        except Exception:
            pass

        def _on_provider_config_missing(payload: Any) -> None:
            guilogger.warning(
                f"Orchestrator detected missing provider config: {payload}"
            )
            try:
                self.event_bus.publish(
                    "ui.notification",
                    {
                        "level": "error",
                        "message": "No provider configured. Open settings to connect LM Studio or Ollama.",
                    },
                )
            except Exception:
                pass

        def _on_provider_status_changed(payload: Any) -> None:
            guilogger.info(f"Orchestrator: provider status changed: {payload}")
            try:
                if (
                    isinstance(payload, dict)
                    and payload.get("status") == "disconnected"
                ):
                    self.event_bus.publish(
                        "ui.notification",
                        {
                            "level": "warning",
                            "message": f"Provider {payload.get('provider')} is disconnected.",
                        },
                    )
            except Exception:
                pass

        def _on_provider_model_missing(payload: Any) -> None:
            guilogger.warning(f"Provider model missing: {payload}")
            try:
                if isinstance(payload, dict):
                    self.event_bus.publish(
                        "ui.notification",
                        {
                            "level": "warning",
                            "message": f"Model {payload.get('requested')} missing on provider {payload.get('provider')}",
                        },
                    )
            except Exception:
                pass

        try:
            self.event_bus.subscribe(
                "provider.config.missing", _on_provider_config_missing
            )
            self.event_bus.subscribe(
                "provider.status.changed", _on_provider_status_changed
            )
            self.event_bus.subscribe(
                "provider.model.missing", _on_provider_model_missing
            )
        except Exception:
            pass

        def _on_models_probing_started(payload: Any) -> None:
            guilogger.info(f"Orchestrator: provider models probing started: {payload}")
            try:
                self.event_bus.publish("orchestrator.models.check.started", payload)
            except Exception:
                pass

        def _on_models_probing_completed(payload: Any) -> None:
            guilogger.info(
                f"Orchestrator: provider models probing completed: {payload}"
            )
            try:
                self.event_bus.publish("orchestrator.models.check.completed", payload)
            except Exception:
                pass

        def _on_models_probing_failed(payload: Any) -> None:
            guilogger.error(f"Orchestrator: provider models probing failed: {payload}")
            try:
                self.event_bus.publish("orchestrator.models.check.failed", payload)
            except Exception:
                pass

        try:
            self.event_bus.subscribe(
                "provider.models.probing_started", _on_models_probing_started
            )
            self.event_bus.subscribe(
                "provider.models.probing_completed", _on_models_probing_completed
            )
            self.event_bus.subscribe(
                "provider.models.probing_failed", _on_models_probing_failed
            )
        except Exception:
            pass

        # GAP 1: Respond to session.request_state with session.hydrated so the
        # TUI can render restored conversation history on mount.
        def _on_session_request_state(payload: Any) -> None:
            try:
                session_id = (
                    payload.get("session_id") if isinstance(payload, dict) else None
                )
                history = []
                try:
                    if hasattr(self, "msg_mgr") and self.msg_mgr:
                        history = list(self.msg_mgr.messages or [])
                except Exception:
                    pass
                self.event_bus.publish(
                    "session.hydrated",
                    {
                        "session_id": session_id
                        or getattr(self, "_current_task_id", "default"),
                        "messageHistory": history,
                        "currentTask": getattr(self, "_current_task", ""),
                        "workingDir": str(self.working_dir),
                    },
                )
            except Exception:
                pass

        try:
            self.event_bus.subscribe("session.request_state", _on_session_request_state)
        except Exception:
            pass

        # async check in background
        # Run background model check in a daemon thread to avoid blocking during init
        try:
            import threading as _threading

            _threading.Thread(target=self._background_model_check, daemon=True).start()
        except Exception:
            # Fallback to synchronous call if threading fails for some reason
            try:
                self._background_model_check()
            except Exception:
                pass

        # Initial publish of current config
        self._publish_active_config()

        # Phase 4: Initialize Token Budget Monitor and Context Controller
        from src.core.orchestration.token_budget import get_token_budget_monitor

        self.token_monitor = get_token_budget_monitor()

        from src.core.context.context_controller import get_context_controller

        self.context_controller = get_context_controller(
            max_tokens=message_max_tokens or 6000
        )

        # Phase 3: Initialize Preview Service for diff previews
        from src.core.orchestration.preview_service import get_preview_service

        self.preview_service = get_preview_service(str(self.working_dir))
        self._pending_preview_id: Optional[str] = None

        # Plan Mode: blocks write tools until user approves the plan
        from src.core.orchestration.plan_mode import PlanMode

        self.plan_mode = PlanMode(orchestrator=self)
        self._plan_approval_event: Optional[asyncio.Event] = None
        self._plan_approved: bool = False

        # Explore Mode: restricts all tool execution to the analyst read-only set.
        # Mirrors opencode's 'explore' agent — no writes, no test execution, no delegation.
        # Set self.explore_mode = True to activate; the execute_tool guard enforces it.
        self.explore_mode: bool = False

        # Role management: current_role restricts tool access; role_manager provides
        # system prompts per role. Both are Optional — set by subagent/delegation code.
        self.current_role: Optional[str] = None
        self.role_manager: Optional[Any] = None

        # Permission gate: used by execute_tool to wait for user approval
        # of destructive tools (delete_file, run_bash, etc.)
        # S7-B: type updated from threading.Event to AsyncGate (non-polling)
        from src.core.orchestration.approval_gate import AsyncGate as _AsyncGate

        self._permission_gate: Optional[_AsyncGate] = None
        self._permission_granted: bool = False

        def _on_tool_permission_granted(payload: Any) -> None:
            # TUI-04: resolve the module-level gate by tool_id when provided
            _tid = str(payload.get("tool_id", "")) if isinstance(payload, dict) else ""
            if _tid:
                resolve_tool_gate(_tid, approved=True)
            # Legacy single-gate fallback
            gate = self._permission_gate
            if gate is not None:
                self._permission_granted = True
                gate.set()

        def _on_tool_permission_denied(payload: Any) -> None:
            _tid = str(payload.get("tool_id", "")) if isinstance(payload, dict) else ""
            if _tid:
                resolve_tool_gate(_tid, approved=False)
            gate = self._permission_gate
            if gate is not None:
                self._permission_granted = False
                gate.set()

        try:
            self.event_bus.subscribe(
                "tool.permission_granted", _on_tool_permission_granted
            )
            self.event_bus.subscribe(
                "tool.permission_denied", _on_tool_permission_denied
            )
        except Exception:
            pass

        # TUI-03: subscribe to bash approval responses from the TUI
        def _on_bash_approval_granted(payload: dict) -> None:
            resolve_bash_gate(str(payload.get("tool_id", "")), approved=True)

        def _on_bash_approval_denied(payload: dict) -> None:
            resolve_bash_gate(str(payload.get("tool_id", "")), approved=False)

        try:
            self.event_bus.subscribe("bash.approval_granted", _on_bash_approval_granted)
            self.event_bus.subscribe("bash.approval_denied", _on_bash_approval_denied)
        except Exception:
            pass

        # D-10: TUI-05 diff-preview subscriptions now delegated to PreviewCoordinator.
        from src.core.orchestration.preview_coordinator import PreviewCoordinator

        self.preview_coordinator = PreviewCoordinator()
        try:
            self.preview_coordinator.attach(self.event_bus)
        except Exception:
            pass

        # D-10: SessionCostTracker — owns usage buffer and session cost estimation.
        from src.core.orchestration.session_cost_tracker import SessionCostTracker
        from src.core.orchestration.project_settings import (
            get_active_settings as _get_ps,
        )

        _active_ps = _get_ps()
        _budget_ceiling = (
            _active_ps.budget_ceiling_usd if _active_ps is not None else None
        )
        self.cost_tracker = SessionCostTracker(
            working_dir=self.working_dir,
            event_bus=self.event_bus,
            budget_ceiling_usd=_budget_ceiling,
        )

        # D-10: ToolExecutionService — owns permission checks and hook dispatch.
        # Hook runner is wired after tool_hooks is available.
        from src.core.orchestration.tool_execution_service import ToolExecutionService

        _hook_runner = None
        try:
            from src.core.orchestration.tool_hooks import ToolHookRunner

            _hook_runner = ToolHookRunner()
        except Exception:
            pass
        self.tool_execution_service = ToolExecutionService(
            registry=self.tool_registry,
            event_bus=self.event_bus,
            hook_runner=_hook_runner,
        )

        # MCP STDIO server (Step 9): instantiated but not started by default.
        # Call start_mcp_server() explicitly to enable IDE integration.
        self._mcp_server = None

    async def start_mcp_server(self) -> None:
        """Start the MCP STDIO server for IDE integration (JSON-RPC over stdin/stdout).

        Call this from the event loop when running in headless/server mode:
            await orchestrator.start_mcp_server()
        """
        try:
            from src.core.orchestration.mcp_stdio_server import MCPStdioServer

            self._mcp_server = MCPStdioServer(orchestrator=self)
            logger.info("Orchestrator: starting MCP STDIO server")
            try:
                self.event_bus.publish(
                    "mcp.server.status", {"running": True, "count": 1}
                )
            except Exception:
                pass
            await self._mcp_server.run_async()
        except Exception as e:
            logger.error(f"Orchestrator: MCP STDIO server error: {e}")
        finally:
            self._mcp_server = None
            try:
                self.event_bus.publish(
                    "mcp.server.status", {"running": False, "count": 0}
                )
            except Exception:
                pass

    def _publish_active_config(self):
        provider = "None"
        model = "None"
        try:
            if self._adapter:
                if hasattr(self._adapter, "provider") and isinstance(
                    self._adapter.provider, dict
                ):
                    provider = (
                        self._adapter.provider.get("name")
                        or self._adapter.provider.get("type")
                        or "None"
                    )
                if (
                    hasattr(self._adapter, "models")
                    and isinstance(self._adapter.models, list)
                    and self._adapter.models
                ):
                    model = self._adapter.models[0]
                elif (
                    hasattr(self._adapter, "default_model")
                    and self._adapter.default_model
                ):
                    model = self._adapter.default_model
        except Exception:
            pass

        if hasattr(self, "event_bus"):
            self.event_bus.publish(
                "model.routing",
                {
                    "selected": model,
                    "provider": provider,
                    "available_models": getattr(self._adapter, "models", [])
                    if self._adapter
                    else [],
                },
            )

    @property
    def adapter(self):
        return self._adapter

    @adapter.setter
    def adapter(self, value):
        self._adapter = value
        self._publish_active_config()

    # Phase 4: Budget status for TUI display
    def get_budget_status(self, session_id: str = "default") -> Dict[str, Any]:
        """Get current token budget status for UI display."""
        budget = self.token_monitor.get_budget(session_id)
        context_status = self.context_controller.get_budget_status()
        return {
            "token_budget": {
                "used_tokens": budget.used_tokens,
                "max_tokens": budget.max_tokens,
                "usage_ratio": budget.usage_ratio,
                "should_compact": budget.should_compact,
                "should_warn": budget.should_warn,
                "current_turn": budget.current_turn,
            },
            "context_budget": context_status,
            "usage_ratio": budget.usage_ratio,
            "used_tokens": budget.used_tokens,
            "max_tokens": budget.max_tokens,
        }

    # HR-5 fix: import canonical dangerous-pattern list from _security.py.
    # Single source of truth — no local copy that could fall behind.
    try:
        from src.tools._security import DANGEROUS_PATTERNS as _dp

        _BASH_DANGEROUS_PATTERNS: list = list(_dp)
    except Exception:  # pragma: no cover
        _BASH_DANGEROUS_PATTERNS = [
            "&&",
            "||",
            ";",
            "|",
            ">",
            ">>",
            "<",
            "$(",
            "`",
            "rm -rf",
            "rm -r",
            "rm -f",
            "del ",
            "format ",
            "shutdown",
            "reboot",
            "halt",
            "poweroff",
            "git push",
        ]

    def preflight_check(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        name_raw = tool_call.get("name")
        if not isinstance(name_raw, str):
            return {"ok": False, "error": "Tool name must be a string."}
        name = name_raw
        args = tool_call.get("arguments", {})

        tool = self.tool_registry.get(name)
        if not tool:
            # TOOLS-03: structured tool-repair response instead of bare error string.
            # Include a short list of the closest-sounding registered tool names so
            # the LLM can self-correct without a full round-trip.
            _all_tool_names = (
                sorted(self.tool_registry.tools.keys())
                if hasattr(self.tool_registry, "tools")
                else []
            )
            import difflib as _dl

            _closest = _dl.get_close_matches(name, _all_tool_names, n=5, cutoff=0.4)
            return {
                "ok": False,
                "error": "tool_not_found",
                "tool_name": name,
                "message": (
                    f"Tool '{name}' is not registered. "
                    + (
                        f"Did you mean one of: {', '.join(_closest)}?"
                        if _closest
                        else "Check the tool name spelling."
                    )
                ),
                "suggestions": _closest,
            }

        # HR-5 fix: validate bash commands at preflight time (defence-in-depth).
        # file_tools.bash() also checks these, but catching them here provides
        # an earlier rejection path and a consistent error source.
        if name == "bash":
            command = args.get("command") or args.get("cmd") or ""
            if command:
                import re as _re

                cmd_normalised = _re.sub(r"\s+", " ", str(command)).lower()
                for pattern in self._BASH_DANGEROUS_PATTERNS:
                    if pattern in cmd_normalised:
                        return {
                            "ok": False,
                            "error": (
                                f"Preflight: bash command contains dangerous pattern "
                                f"'{pattern}'. No shell operators or destructive commands allowed."
                            ),
                        }

        path_arg = args.get("path") or args.get("file_path")
        if path_arg and "write" in tool.get("side_effects", []):
            try:
                # Resolve the path and see if it's inside working_dir
                target_path = (Path(self.working_dir or ".") / path_arg).resolve()
                work_dir = Path(self.working_dir or ".").resolve()
                if not target_path.is_relative_to(work_dir):
                    return {
                        "ok": False,
                        "error": f"Path '{path_arg}' is outside working directory.",
                    }
            except Exception as e:
                return {"ok": False, "error": f"Invalid path: {e}"}

        return {"ok": True}

    def _compact_messages(self, messages: list) -> str:
        """
        Callback passed to MessageManager for inline context compaction.

        When the conversation history overflows the token budget,
        MessageManager calls this method with the messages that would be
        dropped.  We generate a prose summary that is injected back into
        the conversation so the agent always has access to prior context.
        """
        try:
            from src.core.memory.distiller import compact_messages_to_prose

            return compact_messages_to_prose(messages, working_dir=self.working_dir)
        except Exception as e:
            guilogger.warning(f"_compact_messages failed (non-fatal): {e}")
            return ""

    def begin_step_transaction(self) -> str:
        """
        Start a step-level atomic transaction.

        Creates a new snapshot group for the current execution step.
        All files written during this step are accumulated into this snapshot.
        Call rollback_step_transaction() to undo all writes in the step.

        Returns:
            The step snapshot ID.
        """
        from datetime import datetime

        step_id = "step_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._step_snapshot_id = step_id
        guilogger.debug(f"begin_step_transaction: started {step_id}")
        return step_id

    def rollback_step_transaction(self) -> dict:
        """
        Rollback all writes made during the current step transaction.

        Restores every file that was written since begin_step_transaction() was called.

        Returns:
            Rollback result dict with keys ok, restored_files, restored_count.
        """
        if not self._step_snapshot_id:
            return {"ok": False, "error": "No active step transaction"}

        snap_id = self._step_snapshot_id
        result = self.rollback_manager.rollback(snap_id)
        if result.get("ok"):
            guilogger.info(
                f"rollback_step_transaction: restored {result.get('restored_count', 0)} "
                f"file(s) from step {snap_id}"
            )
        else:
            guilogger.warning(
                f"rollback_step_transaction: rollback failed for {snap_id}: "
                f"{result.get('error')}"
            )
        self._step_snapshot_id = None
        return result

    def _get_tool_timeout(self, tool_name: str) -> int:
        """Get timeout in seconds for a tool. T9: Tool Timeout Protection."""
        timeout_map = {
            "bash": 60,
            "run_tests": 120,
            "run_linter": 60,
            "syntax_check": 30,
            "run_js_tests": 120,
            "run_ts_check": 120,
            "run_eslint": 60,
            "search_code": 30,
            "grep": 30,
            "edit_file_atomic": 30,
            "find_symbol": 30,
            "list_files": 10,
            "glob": 10,
        }
        return timeout_map.get(tool_name, 30)

    def _normalize_tool_result(self, res: Any) -> Dict[str, Any]:
        """Ensure tool results conform to a minimal contract.
        Accepts various return shapes and normalizes to a dict with either 'status' or 'ok'."""
        try:
            if isinstance(res, dict):
                return res
            return {"status": "ok", "result": res}
        except Exception:
            return {"status": "error", "error": "tool result normalization failed"}

    def execute_tool(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        name_raw = tool_call.get("name")
        if not isinstance(name_raw, str):
            return {"ok": False, "error": "Tool name must be a string."}
        # Normalise tool name aliases so the LLM can use short forms
        try:
            from src.tools.tools_config import TOOL_ALIASES

            name = TOOL_ALIASES.get(name_raw, name_raw)
        except Exception:
            name = name_raw
        args = dict(tool_call.get("arguments", {}))

        # F4: Strip LLM-injected user_approved to prevent WorkspaceGuard bypass.
        # user_approved is enforced at the orchestrator / UI level, not via LLM arguments.
        args.pop("user_approved", None)

        # UX-3: Dry-run mode — intercept write tools and return a preview dict
        # instead of executing. Allows callers to inspect what would happen without
        # modifying the filesystem.
        if getattr(self, "_dry_run", False) and name in WRITE_TOOLS_REQUIRING_READ:
            return {"status": "dry_run", "would_call": name, "args": args}

        # GAP 2: Generate unique tool call ID for ACP compliance
        tool_call_id = f"call_{uuid.uuid4().hex[:8]}"

        # Hard Rule Enforcement: Read before Edit for write tools
        path_arg = args.get("path") or args.get("file_path")
        if path_arg and name in WRITE_TOOLS_REQUIRING_READ:
            try:
                target = Path(self.working_dir or ".") / path_arg
                resolved_path = str(target.resolve())
                # Only enforce read-before-write on pre-existing files.
                # Brand-new files have no prior content to protect.
                if target.exists() and resolved_path not in self._session_read_files:
                    # UP-1 fix: unified wording — execution_node uses the same
                    # message so the LLM receives a consistent correction signal.
                    return {
                        "ok": False,
                        "error": (
                            f"Security/Logic violation: You must read '{path_arg}' "
                            f"before writing to it. Use read_file first to inspect "
                            f"the current content."
                        ),
                    }
            except Exception:
                pass

        # GAP-S2: Workspace scope guard — block writes to files outside the plan's
        # affected_files set.  Only active when:
        #   (a) the tool is a write-family tool, AND
        #   (b) _affected_files is non-empty (i.e. a plan has been produced), AND
        #   (c) the target path is NOT in the allowed set.
        # When _affected_files is empty/None the guard is inactive (pre-plan fast-path).
        if path_arg and name in WRITE_TOOLS_REQUIRING_READ:
            try:
                _af = getattr(self, "_affected_files", None) or []
                if _af:
                    # Normalise a path to a workdir-relative form for comparison.
                    # Strategy: strip the workdir prefix first (preserving the leading
                    # slash while comparing), then strip any remaining leading slashes.
                    _workdir = str(self.working_dir or ".").rstrip("/\\")

                    def _norm(p: str) -> str:
                        p = str(p)
                        # Strip workdir prefix (absolute or relative match).
                        if p.startswith(_workdir + "/") or p.startswith(
                            _workdir + "\\"
                        ):
                            p = p[len(_workdir) + 1 :]
                        elif p.lstrip("/\\").startswith(_workdir.lstrip("/\\") + "/"):
                            # Handle mismatched leading-slash forms
                            _stripped = p.lstrip("/\\")
                            _wd_rel = _workdir.lstrip("/\\")
                            p = _stripped[len(_wd_rel) + 1 :]
                        # Strip any remaining leading separators.
                        return p.lstrip("/\\")

                    _target_norm = _norm(path_arg)
                    # Build a set of normalised allowed paths for O(1) lookup.
                    _af_set = {_norm(str(_f)) for _f in _af}
                    if _target_norm not in _af_set:
                        # Publish scope violation event for observability.
                        try:
                            from src.core.orchestration.event_bus import get_event_bus

                            get_event_bus().publish(
                                "scope.violation",
                                {
                                    "tool": name,
                                    "path": path_arg,
                                    "allowed": list(_af_set),
                                },
                            )
                        except Exception:
                            pass
                        return {
                            "ok": False,
                            "error": (
                                f"File '{path_arg}' is outside the task scope. "
                                f"The current plan authorises writes to: {sorted(_af_set)}. "
                                "Use ask_user to confirm expanding the scope."
                            ),
                        }
            except Exception:
                pass  # scope guard must never block on internal errors

        # P4-4: Block write tools when plan mode is active (plan not yet approved).
        # _plan_mode_approved is set by execution_node from AgentState before each call.
        try:
            from src.core.orchestration.plan_mode import PlanMode

            _pm = getattr(self, "plan_mode", None)
            if (
                _pm
                and getattr(_pm, "enabled", False)
                and name in PlanMode.BLOCKED_TOOLS
            ):
                _approved = getattr(self, "_plan_mode_approved", None)
                if _approved is not True:
                    return {
                        "ok": False,
                        "error": (
                            f"Tool '{name}' is blocked: the current plan has not been "
                            "approved yet. Await user approval before making file changes."
                        ),
                    }
        except Exception:
            pass  # never block on import/logic errors

        # Explore Mode guard: when active, only analyst read-only tools are permitted.
        # Rejects any write/exec/delegation tool with a clear error message.
        if getattr(self, "explore_mode", False):
            try:
                from src.core.orchestration.role_config import is_tool_allowed_for_role

                if not is_tool_allowed_for_role(name, "analyst"):
                    return {
                        "ok": False,
                        "error": (
                            f"Explore mode is active: tool '{name}' is not permitted. "
                            "Only read-only exploration tools (read_file, glob, grep, "
                            "find_symbol, bash, etc.) are allowed in explore mode."
                        ),
                    }
            except Exception:
                pass  # never block on import errors

        # TASK-01: PermissionLevel-driven gate.
        # DANGER and PROMPT tools require explicit user approval unless the agent
        # is running in autonomous mode (--autonomous / is_autonomous()).
        # The legacy PERMISSION_REQUIRED_TOOLS set is kept for backward compat
        # but the gate now also fires for any tool classified as DANGER/PROMPT.
        _needs_gate = name in PERMISSION_REQUIRED_TOOLS
        if not _needs_gate:
            try:
                from src.tools.tools_config import get_tool_permission, PermissionLevel

                _perm = get_tool_permission(name)
                if _perm in (PermissionLevel.DANGER, PermissionLevel.PROMPT):
                    _needs_gate = True
            except Exception:
                pass  # never block on import errors

        # TASK-20: Active permission mode check.
        # When --permission-mode is set, block any tool whose required level is
        # more permissive than the active mode.  Ordering (least → most permissive):
        #   READ_ONLY < WORKSPACE_WRITE < DANGER < PROMPT < ALLOW
        _PERM_ORDER = {
            "read_only": 0,
            "workspace_write": 1,
            "danger": 2,
            "prompt": 3,
            "allow": 4,
        }
        try:
            from src.tools.tools_config import (
                get_active_permission_mode,
                get_tool_permission,
                PermissionLevel,
            )

            _active_mode = get_active_permission_mode()
            if _active_mode is not None:
                _tool_perm = get_tool_permission(name)
                _active_rank = _PERM_ORDER.get(_active_mode.value, 99)
                _tool_rank = _PERM_ORDER.get(_tool_perm.value, 99)
                # Block if the tool requires more privilege than the active mode allows.
                # READ_ONLY mode (rank=0) only allows rank-0 (READ_ONLY) tools.
                if _tool_rank > _active_rank:
                    return {
                        "ok": False,
                        "error": (
                            f"Tool '{name}' requires '{_tool_perm.value}' permission "
                            f"but active permission mode is '{_active_mode.value}'."
                        ),
                    }
        except Exception:
            pass  # never block on import errors

        if _needs_gate:
            # Skip gate entirely when running autonomously
            _autonomous = False
            try:
                from src.tools.tools_config import is_autonomous

                _autonomous = is_autonomous()
            except Exception:
                pass

            if not _autonomous:
                try:
                    # TUI-04: use module-level gate registry so each tool call gets its
                    # own tool_id and the TUI can target the correct approval request.
                    _t4_id = f"{uuid.uuid4().hex[:8]}"
                    _t4_ev = register_tool_gate(_t4_id)
                    # SPAWN-W5: For delegate_task, also fire the specialized spawn event
                    # so the TUI can show a spawn-specific confirmation dialog.
                    if name == "delegate_task":
                        try:
                            self.event_bus.publish(
                                "spawn.permission_required",
                                {
                                    "tool": name,
                                    "role": args.get("role", ""),
                                    "task": str(args.get("subtask_description", ""))[
                                        :200
                                    ],
                                    "tool_id": _t4_id,
                                },
                            )
                        except Exception:
                            pass
                    self.event_bus.publish(
                        "tool.permission_required",
                        {"tool": name, "args": args, "tool_id": _t4_id},
                    )
                    granted = _t4_ev.wait(timeout=120.0)
                    if not granted or _t4_id in _tool_denied:
                        _tool_denied.discard(_t4_id)
                        return {
                            "ok": False,
                            "error": f"Tool '{name}' was denied by the user.",
                        }
                except Exception as _perm_exc:
                    guilogger.warning(
                        f"Permission gate error for '{name}': {_perm_exc}"
                    )

        # SPAWN-W2: allowed_tools enforcement in delegated context.
        # When an active_agent is set (delegated execution), reject tools that are
        # outside the AgentDefinition.allowed_tools allowlist or inside denied_tools.
        try:
            _active_agent_spawn = getattr(self, "active_agent", None)
            if (
                _active_agent_spawn is not None
                and not _active_agent_spawn.is_tool_permitted(name)
            ):
                _write_permission_audit(
                    self.working_dir, name, args, "deny", "spawn_allowed_tools"
                )
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' is not permitted for the active delegated agent "
                        f"(allowed_tools restriction). Check the agent's allowed_tools list."
                    ),
                }
        except Exception:
            pass

        # PERM-W4: Per-agent permission override.
        # When the orchestrator has an active_agent with permission_rules, merge them
        # with the global policy and apply the combined result.
        try:
            _active_agent = getattr(self, "active_agent", None)
            if _active_agent is not None:
                _global_policy = getattr(self, "permission_policy", None)
                _merged_policy = _active_agent.get_merged_policy(_global_policy)
                if _merged_policy is not None and _merged_policy.is_denied(name):
                    _perm_w4_decision = "deny"
                    _perm_audit_reason = "agent_permission_rules"
                    _write_permission_audit(
                        self.working_dir, name, args, "deny", _perm_audit_reason
                    )
                    return {
                        "ok": False,
                        "error": (
                            f"Tool '{name}' is denied by the active agent's permission rules."
                        ),
                    }
        except Exception:
            pass  # never block on import/logic errors

        # PERM-W5: Permission audit log — record allow decision for tools that
        # passed all gates.  Deny decisions are recorded above and in the gate block.
        try:
            _write_permission_audit(
                self.working_dir, name, args, "allow", "passed_all_gates"
            )
        except Exception:
            pass

        # Publish tool.execute.start AFTER all gate checks so the TUI is only
        # notified about tools that have actually been approved and will run.
        # (GAP 2: ACP sessionUpdate schema)
        try:
            self.event_bus.publish(
                "tool.execute.start",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "title": name,
                    "status": "in_progress",
                    "rawInput": args,
                    "workdir": str(self.working_dir),
                },
            )
        except Exception:
            pass

        tool = self.tool_registry.get(name)
        if not tool:
            return {"ok": False, "error": f"Tool '{name}' not found."}

        try:
            import inspect

            sig = inspect.signature(tool["fn"])
            if "workdir" in sig.parameters:
                args["workdir"] = Path(self.working_dir or ".")

            # SPAWN-W1: Expose this orchestrator as the parent context for tools that
            # spawn subagents (delegate_task).  Uses a ContextVar so the reference is
            # scoped to this call stack and doesn't leak across threads.
            try:
                from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                _orch_token = _PARENT_ORCHESTRATOR_VAR.set(self)
            except Exception:
                _orch_token = None

            # Role enforcement: if orchestrator has current_role, restrict certain tools
            current_role = getattr(self, "current_role", None)
            if current_role:
                from src.core.orchestration.role_config import is_tool_allowed_for_role

                if not is_tool_allowed_for_role(name, current_role):
                    try:
                        if _orch_token is not None:
                            from src.tools.subagent_tools import (
                                _PARENT_ORCHESTRATOR_VAR,
                            )

                            _PARENT_ORCHESTRATOR_VAR.reset(_orch_token)
                    except Exception:
                        pass
                    return {
                        "ok": False,
                        "error": f"Tool '{name}' is not permitted for role '{current_role}'",
                    }

            # Sandbox validation for write operations — C2 fix: validate the NEW content
            # being written, not the pre-existing file.  The previous approach copied the
            # workspace to a temp dir and ran ast.parse on the OLD file, which always passed.
            # Now we extract the new content from the tool args and parse it directly.
            # HR-2/MC-3 fix: extend to JS/TS using `node --check` (syntax only, no eval).
            if "write" in tool.get("side_effects", []) and path_arg:
                try:
                    new_content: str | None = args.get("content")
                    if new_content and isinstance(new_content, str):
                        if path_arg.endswith(".py"):
                            import ast as _ast

                            try:
                                _ast.parse(new_content)
                            except SyntaxError as _syn:
                                return {
                                    "ok": False,
                                    "error": f"Sandbox validation error: new content has a syntax error at line {_syn.lineno}: {_syn.msg}",
                                }
                        elif path_arg.endswith(
                            (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
                        ):
                            # HR-2/MC-3: use Node.js --check flag for JS/TS syntax validation.
                            # node --check parses without executing — safe and fast (~50 ms).
                            # Falls back silently if Node is not installed.
                            import subprocess as _sp
                            import tempfile as _tf

                            try:
                                _node = _sp.run(
                                    ["node", "--version"],
                                    capture_output=True,
                                    timeout=3,
                                )
                                if _node.returncode == 0:
                                    # MED-4 fix: initialize _tmp_path before the try block
                                    # so the finally cleanup never raises NameError.
                                    _tmp_path = None
                                    with _tf.NamedTemporaryFile(
                                        suffix=".js",
                                        mode="w",
                                        delete=False,
                                        encoding="utf-8",
                                    ) as _tmp:
                                        _tmp.write(new_content)
                                        _tmp_path = _tmp.name
                                    try:
                                        _check = _sp.run(
                                            ["node", "--check", _tmp_path],
                                            capture_output=True,
                                            text=True,
                                            timeout=10,
                                        )
                                        if _check.returncode != 0:
                                            _err = (
                                                _check.stderr
                                                or _check.stdout
                                                or "syntax error"
                                            ).strip()
                                            return {
                                                "ok": False,
                                                "error": f"Sandbox validation error: JS/TS syntax error: {_err[:300]}",
                                            }
                                    finally:
                                        try:
                                            import os as _os

                                            if _tmp_path:
                                                _os.unlink(_tmp_path)
                                        except Exception:
                                            pass
                            except (FileNotFoundError, _sp.TimeoutExpired):
                                pass  # node not installed or timed out — allow write
                except Exception as e:
                    # Fail-closed — do not allow writes if validation itself crashes.
                    guilogger.error(f"Sandbox validation failed (fail-closed): {e}")
                    return {
                        "ok": False,
                        "error": f"Sandbox validation aborted: {str(e)}. "
                        f"Write operation blocked for safety.",
                    }

            # Step B: Snapshot files before write operations for automated rollback.
            # If a step-level transaction is active, accumulate into it (multi-file
            # atomicity). Otherwise create an individual per-write snapshot.
            if "write" in tool.get("side_effects", []) and path_arg:
                try:
                    if self._step_snapshot_id:
                        # Append to the step-level atomic snapshot
                        self.rollback_manager.append_to_snapshot(
                            self._step_snapshot_id, path_arg
                        )
                        self._current_snapshot_id = self._step_snapshot_id
                        guilogger.debug(
                            f"File added to step snapshot {self._step_snapshot_id}: {path_arg}"
                        )
                    else:
                        # No active transaction — fall back to per-write snapshot
                        self._current_snapshot_id = (
                            self.rollback_manager.snapshot_files(
                                [path_arg], snapshot_id=None
                            )
                        )
                        guilogger.debug(
                            f"Individual snapshot created before write: {path_arg}"
                        )
                except Exception as snap_err:
                    guilogger.warning(f"Snapshot failed (non-blocking): {snap_err}")

            # Track files read for read-before-edit enforcement
            if name == "read_file" and path_arg:
                try:
                    resolved = str((Path(self.working_dir or ".") / path_arg).resolve())
                    self._session_read_files.add(resolved)
                except Exception:
                    pass

            # T9: Tool Timeout Protection — C1 fix: use ThreadPoolExecutor so timeouts
            # work in any thread (SIGALRM only worked in the main thread, which meant all
            # timeouts were silently disabled when the agent ran from the TUI daemon thread).
            timeout_seconds = self._get_tool_timeout(name)

            # Expose orchestrator to batch tool via thread-local so it can dispatch
            # sub-calls back through execute_tool. Only set when actually calling batch.
            if name == "batch":
                try:
                    from src.tools.batch_tools import set_batch_orchestrator

                    set_batch_orchestrator(self)
                except Exception:
                    pass

            # Pre-tool hooks: user-configured shell scripts that can deny the call.
            try:
                _hook_runner = getattr(self, "_tool_hook_runner", None)
                if _hook_runner is None:
                    from src.core.orchestration.tool_hooks import ToolHookRunner

                    _hook_runner = ToolHookRunner(working_dir=self.working_dir)
                    self._tool_hook_runner = _hook_runner
                _hook_result = _hook_runner.run_pre(name, args)
                if not _hook_result.allowed:
                    return {
                        "ok": False,
                        "error": f"Pre-tool hook denied '{name}': {_hook_result.reason}",
                    }
            except Exception:
                pass  # never block on hook infrastructure errors

            try:
                import concurrent.futures as _cf

                if timeout_seconds > 0:
                    # HR-3 fix: reuse the long-lived _tool_executor instead of
                    # creating a new ThreadPoolExecutor per call (~5 ms savings each).
                    _tex = getattr(self, "_tool_executor", None)
                    if _tex is None:
                        _tex = _cf.ThreadPoolExecutor(max_workers=2)
                        self._tool_executor = _tex
                    _future = _tex.submit(tool["fn"], **args)
                    try:
                        res = _future.result(timeout=timeout_seconds)
                    except _cf.TimeoutError:
                        _future.cancel()
                        raise TimeoutError(
                            f"Tool '{name}' timed out after {timeout_seconds} seconds"
                        )
                else:
                    res = tool["fn"](**args)
            except TimeoutError:
                guilogger.warning(f"Tool '{name}' timed out after {timeout_seconds}s")
                # SPAWN-W1: Reset ContextVar on early return paths too.
                try:
                    if _orch_token is not None:
                        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                        _PARENT_ORCHESTRATOR_VAR.reset(_orch_token)
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": f"Tool execution timed out after {timeout_seconds} seconds. "
                    f"Consider breaking down the task into smaller steps.",
                }

            # SPAWN-W1: Reset the parent orchestrator ContextVar after the tool returns.
            try:
                if _orch_token is not None:
                    from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                    _PARENT_ORCHESTRATOR_VAR.reset(_orch_token)
            except Exception:
                pass

            # Normalize the result to a dict contract
            res = self._normalize_tool_result(res)

            # Tool output truncation: prevent individual tool results from consuming
            # excessive context window tokens. Cap string values at 8000 chars and
            # inject a truncation notice so the LLM knows content was cut.
            # Mirrors opencode's Truncate.output() strategy.
            _TOOL_OUTPUT_MAX_CHARS = 8000
            if isinstance(res, dict):
                # Copy once before any mutation — avoids repeated copies inside the loop.
                if any(
                    isinstance(_v, str) and len(_v) > _TOOL_OUTPUT_MAX_CHARS
                    for _v in res.values()
                ):
                    res = dict(res)
                    for _k, _v in list(res.items()):
                        if isinstance(_v, str) and len(_v) > _TOOL_OUTPUT_MAX_CHARS:
                            res[_k] = (
                                _v[:_TOOL_OUTPUT_MAX_CHARS]
                                + f"\n... [truncated: {len(_v) - _TOOL_OUTPUT_MAX_CHARS} chars omitted]"
                            )
                # Also truncate nested result.content (common for read_file)
                _inner = res.get("result")
                if isinstance(_inner, dict) and any(
                    isinstance(_v, str) and len(_v) > _TOOL_OUTPUT_MAX_CHARS
                    for _v in _inner.values()
                ):
                    _inner = dict(_inner)
                    res["result"] = _inner
                    for _k, _v in list(_inner.items()):
                        if isinstance(_v, str) and len(_v) > _TOOL_OUTPUT_MAX_CHARS:
                            _inner[_k] = (
                                _v[:_TOOL_OUTPUT_MAX_CHARS]
                                + f"\n... [truncated: {len(_v) - _TOOL_OUTPUT_MAX_CHARS} chars omitted]"
                            )

            # (O4: set_role handler removed — role_tools not registered; runtime role
            # changes via tool calls are disallowed)

            # ORCH-W4: plan_enter / plan_exit mode transitions.
            # If the tool signals an agent_mode change, update the orchestrator attribute so
            # perception_node picks it up on the next system-prompt rebuild.
            if (
                name in ("plan_enter", "plan_exit")
                and isinstance(res, dict)
                and res.get("ok")
            ):
                _new_mode = res.get("agent_mode", "execution")
                setattr(self, "_agent_mode", _new_mode)
                try:
                    self.event_bus.publish(
                        "agent.mode_changed",
                        {"mode": _new_mode, "tool": name},
                    )
                except Exception:
                    pass

            # Post-tool hooks: fire-and-forget, exit code ignored.
            try:
                _hook_runner = getattr(self, "_tool_hook_runner", None)
                if _hook_runner is not None:
                    _hook_runner.run_post(name, args, res)
            except Exception:
                pass

            # Validate tool contract if registered. If validation fails, return an error to the caller.
            try:
                schema = get_tool_contract(name)
                if schema and isinstance(res, dict):
                    # schema is a pydantic model class; validate either full res or res.get('result')
                    try:
                        schema.model_validate(res)
                    except ValidationError:
                        try:
                            schema.model_validate(res.get("result") or {})
                        except ValidationError as ve:
                            return {
                                "ok": False,
                                "error": f"Tool result failed contract validation: {ve}",
                            }

                else:
                    # Validate general ToolContract wrapper for additional safety
                    try:
                        ToolContract.model_validate(
                            {"tool": name, "args": args, "result": res}
                        )
                    except ValidationError as ve:
                        return {
                            "ok": False,
                            "error": f"Tool result failed contract validation: {ve}",
                        }

                # Telemetry: record invocation counts and latency (best-effort)
                try:
                    import time as _time

                    # Telemetry data for usage tracking
                    # (call_info reserved for future telemetry use)
                    _ts = _time.time()

                    # F17: Accumulate in memory — flushed to usage.json at end of run_agent_once()
                    self._usage_buffer[name] = self._usage_buffer.get(name, 0) + 1
                    self.cost_tracker.record_tool_call(name)

                    # Telemetry: publish event for tool invocation (GAP 2: ACP schema)
                    try:
                        self.event_bus.publish(
                            "tool.invoked",
                            {
                                "sessionUpdate": "tool_call_update",
                                "toolCallId": tool_call_id,
                                "title": name,
                                "status": "invoked",
                                "timestamp": _ts,
                                "workdir": str(self.working_dir),
                            },
                        )
                    except Exception:
                        pass

                    # Trace appending moved to workflow_nodes.py execution_node to avoid duplicates
                    # self._append_execution_trace(call_info)
                except Exception:
                    pass
            except Exception:
                # If pydantic is missing or other issues occur, continue without strict validation
                pass

            # Track state for session rules
            if res.get("status") == "ok" or res.get("ok") is True:
                if name in ["read_file", "fs.read"]:
                    try:
                        resolved_path = str(
                            (Path(self.working_dir or ".") / (path_arg or "")).resolve()  # type: ignore[operator]
                        )
                        self._session_read_files.add(resolved_path)
                    except Exception:
                        pass
                elif "write" in tool.get("side_effects", []):
                    try:
                        resolved_path = str(
                            (Path(self.working_dir or ".") / (path_arg or "")).resolve()  # type: ignore[operator]
                        )
                        self._session_modified_files.add(resolved_path)
                        # Publish file.modified event for UI dashboard
                        try:
                            self.event_bus.publish(
                                "file.modified",
                                {
                                    "path": resolved_path,
                                    "tool": name,
                                    "workdir": str(self.working_dir),
                                },
                            )
                        except Exception:
                            pass
                    except Exception:
                        pass
                elif name == "delete_file" and path_arg:
                    try:
                        resolved_path = str(
                            (Path(self.working_dir or ".") / path_arg).resolve()
                        )
                        # Publish file.deleted event for UI dashboard
                        try:
                            self.event_bus.publish(
                                "file.deleted",
                                {
                                    "path": resolved_path,
                                    "workdir": str(self.working_dir),
                                },
                            )
                        except Exception:
                            pass
                    except Exception:
                        pass

            # Append execution trace for loop detection and auditing
            try:
                import datetime

                entry = {
                    "tool": name,
                    "args": self._normalize_args(args),
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "result_ok": bool(
                        res.get("status") == "ok" or res.get("ok") is True
                    ),
                }
                try:
                    self._append_execution_trace(entry)
                except Exception:
                    pass
            except Exception:
                pass

            # Publish tool.execute.finish event (GAP 2: ACP sessionUpdate schema)
            try:
                formatted = _format_tool_result(res, name)
                self.event_bus.publish(
                    "tool.execute.finish",
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tool_call_id,
                        "title": name,
                        "status": "completed",
                        "content": [{"type": "text", "text": formatted}],
                        "rawOutput": res,
                        "workdir": str(self.working_dir),
                    },
                )
            except Exception:
                pass

            # Phase 4: Publish token budget status for UI dashboard
            try:
                if hasattr(self, "token_monitor"):
                    budget = self.token_monitor.get_budget(
                        session_id=getattr(self, "_current_task_id", "default")
                    )
                    self.event_bus.publish(
                        "token.budget.update",
                        {
                            "used_tokens": budget.used_tokens,
                            "max_tokens": budget.max_tokens,
                            "usage_ratio": budget.usage_ratio,
                            "session_id": getattr(self, "_current_task_id", "default"),
                        },
                    )
                    # TUI-09: also publish canonical token.budget event used by TUI sidebar
                    _used = budget.used_tokens
                    _limit = budget.max_tokens or 32_768
                    _pct = min(100, int(_used / _limit * 100)) if _limit else 0
                    self.event_bus.publish(
                        "token.budget",
                        {
                            "used": _used,
                            "limit": _limit,
                            "percent": _pct,
                            "warning": _pct >= 80,
                        },
                    )
            except Exception:
                pass

            # Phase 3: Publish preview mode events for UI dashboard
            try:
                if hasattr(self, "preview_service") and hasattr(
                    self, "_pending_preview_id"
                ):
                    if self._pending_preview_id:
                        self.event_bus.publish(
                            "preview.pending",
                            {"preview_id": self._pending_preview_id},
                        )
            except Exception:
                pass

            # Step B: Log tool call to SessionStore
            try:
                _safe_args = {
                    k: str(v) if isinstance(v, Path) else v for k, v in args.items()
                }
                self.session_store.add_tool_call(
                    session_id=getattr(self, "_current_task_id", "unknown"),
                    tool_name=name,
                    args=_safe_args,
                    result=res,
                    success=True,
                )
            except Exception:
                pass  # non-fatal

            # GAP 1: Sync session state after tool execution
            self._sync_session_state()

            return {"ok": True, "result": res}
        except Exception as e:
            # Publish tool.execute.error event for UI dashboard (GAP 2: ACP schema)
            try:
                self.event_bus.publish(
                    "tool.execute.error",
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tool_call_id,
                        "title": name,
                        "status": "failed",
                        "content": [{"type": "text", "text": str(e)}],
                        "error": str(e),
                        "workdir": str(self.working_dir),
                    },
                )
            except Exception:
                pass

            # Log failed tool call to SessionStore
            try:
                _safe_args = {
                    k: str(v) if isinstance(v, Path) else v for k, v in args.items()
                }
                self.session_store.add_tool_call(
                    session_id=getattr(self, "_current_task_id", "unknown"),
                    tool_name=name,
                    args=_safe_args,
                    result={"error": str(e)},
                    success=False,
                )
            except Exception:
                pass  # non-fatal

            # GAP 1: Sync session state after tool error
            self._sync_session_state()

            # SPAWN-W1: Reset ContextVar on exception path to prevent leak.
            try:
                _tok = locals().get("_orch_token")
                if _tok is not None:
                    from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                    _PARENT_ORCHESTRATOR_VAR.reset(_tok)
            except Exception:
                pass

            return {"ok": False, "error": str(e)}

    def _read_execution_trace(self) -> list:
        try:
            trace_path = self.working_dir / ".agent-context" / "execution_trace.json"
            if trace_path.exists():
                import json

                return json.loads(trace_path.read_text())
        except Exception:
            pass
        return []

    def _normalize_args(self, a: Any):
        """Normalize args into a JSON-serializable Python structure.
        This ensures consistent comparison in loop detection regardless of
        original arg types (Path, objects, etc.)."""
        try:
            import json

            return json.loads(json.dumps(a, default=str))
        except Exception:
            try:
                return str(a)
            except Exception:
                return None

    def _append_execution_trace(self, entry: dict):
        """Buffer execution trace entries in memory; flush to disk via flush_execution_trace.

        PB-4 fix: previously every tool call wrote the full trace JSON to disk synchronously,
        causing O(n) disk I/O per tool where n is trace length.  Now entries are kept in
        self._execution_trace_buffer and flushed once per task by run_agent_once().
        """
        try:
            # Initialise buffer on first use (handles pickled/restored instances too)
            if not hasattr(self, "_execution_trace_buffer"):
                self._execution_trace_buffer: list = []

            # Compute retry count from recent buffer + persisted trace
            recent = self._execution_trace_buffer[-10:]
            count = 0
            for e in recent:
                try:
                    if e.get("tool") == entry.get("tool") and e.get(
                        "args"
                    ) == self._normalize_args(entry.get("args")):
                        count += 1
                except Exception:
                    if e.get("tool") == entry.get("tool") and e.get(
                        "args"
                    ) == entry.get("args"):
                        count += 1
            entry["retries"] = count
            try:
                entry["args"] = self._normalize_args(entry.get("args"))
            except Exception:
                pass
            self._execution_trace_buffer.append(entry)
        except Exception as e:
            guilogger.error(
                f"Orchestrator: failed to buffer execution trace entry: {e}"
            )

    def flush_execution_trace(self):
        """Flush buffered trace entries to disk once per task (called by run_agent_once)."""
        if (
            not hasattr(self, "_execution_trace_buffer")
            or not self._execution_trace_buffer
        ):
            return
        try:
            trace = self._read_execution_trace()
            trace.extend(self._execution_trace_buffer)
            self._execution_trace_buffer = []
            trace_path = self.working_dir / ".agent-context" / "execution_trace.json"
            import json

            def serializer(obj):
                if isinstance(obj, Path):
                    return str(obj)
                return str(obj)

            trace_path.write_text(json.dumps(trace, indent=2, default=serializer))
        except Exception as e:
            guilogger.error(f"Orchestrator: failed to flush execution trace: {e}")

    def compact_context(self) -> bool:
        """S9-B: Immediately distill the current conversation history.

        Triggers ``distill_context()`` regardless of the token threshold so
        the user can manually free context window space at any time via the
        ``/compact`` TUI slash command.

        Returns:
            True if distillation ran successfully; False on error or no history.
        """
        try:
            from src.core.memory.distiller import distill_context

            history = self.msg_mgr.messages if hasattr(self, "msg_mgr") else []
            if not history:
                return False
            distill_context(
                messages=list(history),
                working_dir=self.working_dir if hasattr(self, "working_dir") else None,
            )
            guilogger.info("compact_context: distillation complete")
            # Publish event for TUI status bar
            try:
                _bus = getattr(self, "event_bus", None)
                if _bus:
                    _bus.publish("context.compacted", {"message": "Context compacted"})
            except Exception:
                pass
            return True
        except Exception as exc:
            guilogger.warning(f"compact_context: distillation failed: {exc}")
            return False

    def _clear_execution_trace(self):
        # Also clear the in-memory buffer so buffered entries don't reappear after clear
        if hasattr(self, "_execution_trace_buffer"):
            self._execution_trace_buffer = []
        try:
            trace_path = self.working_dir / ".agent-context" / "execution_trace.json"
            import json

            trace_path.write_text(json.dumps([], indent=2))
        except Exception as e:
            guilogger.error(f"Orchestrator: failed to clear execution trace: {e}")

    def _publish_session_changes(self):
        """Publish session changes to event bus for sidebar display."""
        try:
            if not self._session_modified_files:
                return

            # Get relative paths for display
            workdir_path = self.working_dir.resolve()
            changes = []
            for f in self._session_modified_files:
                try:
                    fpath = Path(f).resolve()
                    if str(fpath).startswith(str(workdir_path)):
                        rel_path = str(fpath.relative_to(workdir_path))
                    else:
                        rel_path = str(fpath)
                    changes.append({"path": rel_path, "absolute": str(fpath)})
                except Exception:
                    changes.append({"path": str(f), "absolute": str(f)})

            self.event_bus.publish(
                "session.files_changed",
                {
                    "files": changes,
                    "workdir": str(workdir_path),
                    "is_git_repo": _is_git_repo(str(workdir_path)),
                },
            )
        except Exception:
            pass

    def _check_loop_prevention(self, tool_name: Optional[str], tool_args: dict) -> bool:
        if not tool_name:
            return False
        # Load trace and apply a time-windowed, conservative de-duplication strategy.
        # Rationale: block only when the same tool+args is repeatedly attempted within a
        # short timeframe (e.g., 5 minutes) and with at least 3 attempts — this reduces
        # false-positives while still protecting against runaway loops.
        try:
            trace = self._read_execution_trace() or []
        except Exception:
            trace = []

        # PB-4: merge in-memory buffer so loop detection works even before flush
        buffer = getattr(self, "_execution_trace_buffer", [])
        if buffer:
            trace = trace + list(buffer)

        if not trace:
            return False

        # Consider only recent entries within TIME_WINDOW seconds
        TIME_WINDOW = 300
        now_ts = None
        recent_entries = []
        try:
            import datetime

            now_ts = datetime.datetime.now(datetime.timezone.utc)
            for e in reversed(trace):
                ts = e.get("ts")
                if not ts:
                    # If no timestamp, include but don't rely on it for timing
                    recent_entries.append(e)
                    continue
                try:
                    entry_ts = datetime.datetime.fromisoformat(ts)
                except Exception:
                    # try parsing without timezone
                    try:
                        entry_ts = datetime.datetime.fromisoformat(ts + "+00:00")
                    except Exception:
                        recent_entries.append(e)
                        continue
                delta = (now_ts - entry_ts).total_seconds()
                if delta <= TIME_WINDOW:
                    recent_entries.append(e)
                else:
                    break
        except Exception:
            recent_entries = trace[-10:]

        # MED-6 fix: the comment said "3+ matches" but the threshold was `>= 2`
        # (triggers on the 2nd match).  Align threshold with the stated intent.
        # Now count exact matches (tool + args) conservatively: block only if 3+ matches
        exact_count = 0
        for entry in recent_entries:
            try:
                if entry.get("tool") == tool_name and entry.get(
                    "args"
                ) == self._normalize_args(tool_args):
                    exact_count += 1
            except Exception:
                continue
        if exact_count >= 3:
            return True

        # Count tool-only occurrences and require a higher threshold (e.g., 6) to block
        tool_only_count = 0
        for entry in recent_entries:
            try:
                if entry.get("tool") == tool_name:
                    tool_only_count += 1
            except Exception:
                continue
        if tool_only_count >= 6:
            return True

        return False

    def _flush_usage_buffer(self) -> None:
        """F17: Legacy flush method — delegates to SessionCostTracker for backward compatibility.

        .. deprecated::
            Call ``self.cost_tracker.flush()`` directly.  This wrapper exists only to
            avoid breaking any external callers.  Internal call sites in
            ``run_agent_once()`` now call ``cost_tracker.flush()`` directly.
        """
        self.cost_tracker.flush(task_id=getattr(self, "_current_task_id", ""))

    def _create_session_snapshot(self) -> None:
        """Create a session snapshot for resume capability."""
        if not hasattr(self, "lifecycle_manager") or not self._current_task_id:
            return
        try:
            state = {
                "task": "",
                "history": self.msg_mgr.messages if hasattr(self, "msg_mgr") else [],
                "current_step": 0,
                "current_plan": None,
                "verified_reads": list(self._session_read_files),
                "files_read": {},
                "tool_call_count": sum(self._usage_buffer.values())
                if self._usage_buffer
                else 0,
                "rounds": 0,
                "session_id": self._current_task_id,
            }
            snapshot = self.lifecycle_manager.create_snapshot(
                session_id=self._current_task_id,
                state=state,
                metadata={
                    "task_description": self.msg_mgr.messages[0].get("content", "")[
                        :100
                    ]
                    if self.msg_mgr.messages
                    else "",
                },
            )
            self.lifecycle_manager.save_snapshot(snapshot)
            guilogger.info(
                f"Created session snapshot for task: {self._current_task_id}"
            )
        except Exception as e:
            guilogger.warning(f"Failed to create session snapshot: {e}")

    def _ensure_working_dir(self):
        try:
            self.working_dir.mkdir(parents=True, exist_ok=True)

            # Phase 3: Scaffold .agent-context directory
            agent_context_dir = self.working_dir / ".agent-context"
            agent_context_dir.mkdir(parents=True, exist_ok=True)

            task_state_path = agent_context_dir / "TASK_STATE.md"
            if not task_state_path.exists():
                task_state_path.write_text(
                    "# Current Task\n\n# Completed Steps\n\n# Next Step\n"
                )

            active_path = agent_context_dir / "ACTIVE.md"
            if not active_path.exists():
                active_path.write_text("No active goal.")

            trace_path = agent_context_dir / "execution_trace.json"
            if not trace_path.exists():
                import json

                trace_path.write_text(json.dumps([]))

        except Exception as e:
            guilogger.error(
                f"Orchestrator: failed to create working dir {self.working_dir}: {e}"
            )

    def _background_model_check(self):
        try:
            pm = get_provider_manager()
            if pm:
                # First try to use cached models to avoid redundant API calls
                cached = None
                try:
                    cached = pm.get_cached_models("lm_studio")
                except Exception:
                    pass

                # Only call API if no cached models available
                if not cached:
                    adapters = pm.list_providers()
                    if "lm_studio" in adapters:
                        ad = pm.get_provider("lm_studio")
                        if ad and hasattr(ad, "get_models_from_api"):
                            ad.get_models_from_api()

                self.event_bus.publish(
                    "provider.models.cached", {"provider": "lm_studio"}
                )
                self.event_bus.publish(
                    "provider.models.probing.completed", {"provider": "lm_studio"}
                )
        except Exception:
            pass

    def _publish_git_status(self) -> None:
        """TUI-07: Publish git.branch event so the TUI sidebar stays current.

        Called from start_new_task() and after run_agent_once() completes.
        All subprocess errors are silently swallowed — git status is informational.
        """
        import subprocess as _sp

        try:
            branch = _sp.check_output(
                ["git", "branch", "--show-current"],
                cwd=str(self.working_dir),
                text=True,
                timeout=3,
                stderr=_sp.DEVNULL,
            ).strip()
        except Exception:
            return  # not a git repo or git not available

        try:
            status_out = _sp.check_output(
                ["git", "status", "--porcelain"],
                cwd=str(self.working_dir),
                text=True,
                timeout=3,
                stderr=_sp.DEVNULL,
            )
            dirty = bool(status_out.strip())
        except Exception:
            dirty = False

        # Parse ahead/behind counts from `git status -sb` if possible
        ahead = 0
        behind = 0
        try:
            sb_out = _sp.check_output(
                ["git", "status", "-sb"],
                cwd=str(self.working_dir),
                text=True,
                timeout=3,
                stderr=_sp.DEVNULL,
            )
            import re as _re

            m = _re.search(r"\[ahead (\d+)(?:, behind (\d+))?\]", sb_out)
            if not m:
                m = _re.search(r"\[behind (\d+)\]", sb_out)
                if m:
                    behind = int(m.group(1))
            else:
                ahead = int(m.group(1))
                if m.group(2):
                    behind = int(m.group(2))
        except Exception:
            pass

        try:
            self.event_bus.publish(
                "git.branch",
                {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind},
            )
        except Exception:
            pass

    def start_new_task(self) -> str:
        """
        Start a new task by generating a new task ID and clearing per-task state.
        Returns the new task ID.

        ME-2 fix: previously only msg_mgr.messages was cleared; the session read/modified
        file sets and rollback snapshots were left over from the prior task, which could
        cause incorrect read-before-edit guards and stale rollback state.

        GAP 1: Also updates AgentSessionManager for state hydration.
        """
        self._current_task_id = str(uuid.uuid4())[:8]
        try:
            self.msg_mgr.messages = []
        except Exception:
            pass
        # Reset per-task session tracking
        self._session_read_files = set()
        self._session_modified_files = set()
        # Reset read-before-write guardrail state for the new task
        try:
            from src.tools.guardrails import reset_guardrail_state

            reset_guardrail_state()
        except Exception:
            pass
        # Reset execution trace buffer for the new task
        self._execution_trace_buffer: list = []
        # Reset rollback manager snapshot state
        try:
            if hasattr(self, "rollback_manager"):
                self.rollback_manager.current_snapshot = None
                self._current_snapshot_id = None
                self._step_snapshot_id = None
        except Exception:
            pass

        # Reset plan mode state for the new task
        if hasattr(self, "plan_mode") and self.plan_mode:
            self.plan_mode.disable()
        self._plan_approval_event = None
        self._plan_approved = False

        # HR-9 fix: clear stale delegations from the previous task so that
        # memory_sync does not spuriously route to delegation_node on the next run.
        if hasattr(self, "_pending_delegations"):
            self._pending_delegations = []

        # MM-1 fix: Invalidate compaction checkpoint from previous task to
        # prevent cross-task memory contamination.
        try:
            _cp = Path(self.working_dir) / ".agent-context" / "compaction_checkpoint.md"
            if _cp.exists():
                _cp.unlink()
        except Exception:
            pass

        # PB-2 fix: Invalidate ContextBuilder module-level file caches so that
        # role/skill YAML files modified on disk between tasks are re-read rather
        # than served from a process-global stale cache entry.
        try:
            from src.core.context.context_builder import ContextBuilder

            ContextBuilder.clear_cache()
        except Exception:
            pass

        # HR-8 fix: clear stale preview events so wait_for_user_node does not
        # block on a preview that was never confirmed/rejected in the last task.
        try:
            from src.core.orchestration.preview_service import PreviewService

            svc = PreviewService.get_instance()
            svc.pending_previews.clear()
        except Exception:
            pass

        # GAP 1: Update AgentSessionManager for state hydration
        try:
            from src.core.orchestration.agent_session_manager import (
                get_agent_session_manager,
            )

            session_mgr = get_agent_session_manager()
            session_mgr.update_session_state(
                session_id=self._current_task_id,
                task="",
                message_history=[],
                current_plan=[],
                current_step=0,
                provider=self._provider_name or "",
                model=getattr(self, "_model", ""),
                files_read=[],
                files_modified=[],
            )
        except Exception as e:
            guilogger.debug(f"Failed to update session manager: {e}")

        # ORCH-W5: Reset session title so the next task gets its own generated title.
        self._session_title = None
        # ORCH-W4: Reset agent mode to "execution" for the new task.
        self._agent_mode = "execution"

        guilogger.info(f"Started new task with ID: {self._current_task_id}")
        # TUI-07: publish git status so the sidebar branch chip is current
        self._publish_git_status()
        return self._current_task_id

    def restore_continue_state(self, state: dict) -> None:
        """Restore saved conversation state for the /continue workflow.

        Accepts the dict produced by the TUI's _save_state_for_continue and applies
        it to the orchestrator without the caller needing to touch private fields.
        """
        try:
            if hasattr(self, "msg_mgr") and "history" in state:
                history = state["history"]
                if history and hasattr(self.msg_mgr, "messages"):
                    self.msg_mgr.messages = list(history)
            if "session_read_files" in state:
                self._session_read_files = set(state["session_read_files"])
            # Restore AgentState fields used for mid-plan resume (U6)
            last_state = getattr(self, "_last_agent_state", None) or {}
            for key in (
                "current_plan",
                "current_step",
                "working_dir",
                "step_retry_counts",
            ):
                val = state.get(key)
                if val is not None:
                    last_state[key] = val
            self._last_agent_state = last_state
        except Exception as e:
            guilogger.error(f"restore_continue_state failed: {e}")

    def _sync_session_state(self) -> None:
        """Sync current orchestrator state to AgentSessionManager for hydration."""
        try:
            from src.core.orchestration.agent_session_manager import (
                get_agent_session_manager,
            )

            session_mgr = get_agent_session_manager()
            provider_name = ""
            model_name = ""
            try:
                if self._adapter:
                    if hasattr(self._adapter, "provider") and isinstance(
                        self._adapter.provider, dict
                    ):
                        provider_name = (
                            self._adapter.provider.get("name")
                            or self._adapter.provider.get("type")
                            or ""
                        )
                    if hasattr(self._adapter, "default_model"):
                        model_name = self._adapter.default_model or ""
                    elif (
                        hasattr(self._adapter, "models")
                        and isinstance(self._adapter.models, list)
                        and self._adapter.models
                    ):
                        model_name = self._adapter.models[0]
            except Exception:
                pass

            session_mgr.update_session_state(
                session_id=self._current_task_id or "default",
                message_history=self.msg_mgr.messages
                if hasattr(self, "msg_mgr")
                else [],
                files_read=list(self._session_read_files),
                files_modified=list(self._session_modified_files),
                provider=provider_name,
                model=model_name,
            )
        except Exception as e:
            guilogger.debug(f"Failed to sync session state: {e}")

    def get_current_task_id(self) -> Optional[str]:
        """Get the current task ID."""
        return self._current_task_id

    def get_file_lock_manager(self):
        """Get the FileLockManager for PRSW coordination."""
        return self.file_lock_manager

    def approve_plan(self) -> None:
        """Called by TUI when user approves the pending plan."""
        self._plan_approved = True
        if self.plan_mode:
            self.plan_mode.disable()
        if self._plan_approval_event:
            self._plan_approval_event.set()
        guilogger.info("Orchestrator: plan approved by user")

    def reject_plan(self) -> None:
        """Called by TUI when user rejects the pending plan."""
        self._plan_approved = False
        if self._plan_approval_event:
            self._plan_approval_event.set()
        guilogger.info("Orchestrator: plan rejected by user")

    async def wait_for_plan_approval(self) -> bool:
        """Suspend until user approves or rejects the plan. Returns True if approved."""
        self._plan_approval_event = asyncio.Event()
        self._plan_approved = False
        await self._plan_approval_event.wait()
        return self._plan_approved

    def get_tools_for_role(self, role: str) -> List[Dict[str, Any]]:
        """Return the tool list filtered to the toolset appropriate for *role*.

        OE-5 fix: ToolsetManager existed but was never wired; every node passed
        the full registry (~40 tools) to the LLM regardless of what the role
        actually needs.  This method returns only the tools in the matching
        toolset, falling back to the full registry when the toolset is unknown or
        its tools are not all registered (avoids silently stripping required tools).

        Usage in nodes::

            tools_list = orchestrator.get_tools_for_role("debugger")
            # → filtered to debug toolset: bash, read_file, edit_file, run_tests …
        """
        try:
            try:
                from src.tools.toolsets.loader import (
                    get_toolset_for_role,
                    get_tools_for_toolset,
                )
            except ImportError:
                from src.config.toolsets.loader import (
                    get_toolset_for_role,
                    get_tools_for_toolset,
                )

            toolset_name = get_toolset_for_role(role)
            toolset_tool_names = get_tools_for_toolset(toolset_name)
            if not toolset_tool_names:
                # Unknown toolset — fall back to full registry
                raise ValueError(f"empty toolset for role={role!r}")
            all_tools = self.tool_registry.tools
            # Include only tools that are actually registered
            filtered = [
                {"name": n, "description": all_tools[n].get("description", "")}
                for n in toolset_tool_names
                if n in all_tools
            ]
            # Safety: if fewer than 3 tools matched, fall back to full list
            # (toolset YAML may be stale relative to the registry)
            if len(filtered) < 3:
                raise ValueError(
                    f"toolset {toolset_name!r} matched too few registered tools"
                )
            return filtered
        except Exception as _e:
            # SCAN-4 fix: log a warning so operators can detect toolset misconfiguration.
            # The graceful fallback to the full registry is intentional (nodes must always
            # receive a usable tool list), but silent failures mask YAML staleness issues.
            guilogger.warning(
                f"get_tools_for_role({role!r}): toolset lookup failed ({_e}); "
                "falling back to full tool registry"
            )
            return [
                {"name": n, "description": m.get("description", "")}
                for n, m in self.tool_registry.tools.items()
            ]

    def run_agent_once(
        self,
        system_prompt_name: Optional[str],
        messages: List[Dict[str, Any]],
        tools: Dict[str, Any],
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Invokes the LangGraph cognitive pipeline to execute the task.
        """
        # Store cancel_event on orchestrator instance so nodes can access it via getattr
        self.cancel_event = cancel_event

        # F16: Reset session read-file tracking at the start of each new task so reads
        # from a previous task cannot bypass the read-before-edit guard in a new task.
        self._session_read_files = set()
        # F17: Reset per-task usage buffer; will be flushed to disk once at task end.
        self._usage_buffer = {}
        # D-10: Reset the SessionCostTracker buffer and idempotency guard for this turn.
        self.cost_tracker.reset()
        self.tool_execution_service.reset_idempotency()

        # Check if canceled before starting
        if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
            return {
                "ok": False,
                "error": "canceled_before_start",
                "assistant_message": "Task was canceled before starting.",
            }

        prompt = ""
        if (
            messages
            and isinstance(messages, list)
            and messages[-1].get("role") == "user"
        ):
            prompt = messages[-1].get("content", "")

        # SES-W2: Persist user prompt to SessionStore transcript.
        if prompt:
            try:
                self.session_store.add_message(
                    session_id=getattr(self, "_current_task_id", "unknown"),
                    role="user",
                    content=prompt,
                )
            except Exception:
                pass

        # ORCH-W5: Generate session title via the internal "title" agent (one-shot,
        # no tool loop).  Only on the first turn (_session_title not yet set) so we
        # don't regenerate on every continuation call.  Fire-and-forget in a daemon
        # thread so it never blocks the main pipeline.
        if prompt and not getattr(self, "_session_title", None):
            import threading as _threading

            def _gen_title(p: str) -> None:
                try:
                    from src.core.memory.distiller import generate_session_title

                    _t = generate_session_title(p)
                    if _t:
                        self._session_title = _t
                        self.event_bus.publish("session.title_generated", {"title": _t})
                except Exception:
                    pass

            _t_thread = _threading.Thread(
                target=_gen_title, args=(prompt,), daemon=True
            )
            _t_thread.start()

        from .agent_brain import load_system_prompt
        from src.core.orchestration.graph.builder import _get_compiled_graph

        # 1. Prepare Initial State
        # Ensure current model routing is published in case tests replace the event_bus after instantiation
        try:
            self._publish_active_config()
        except Exception:
            pass

        full_system_prompt = (
            load_system_prompt(system_prompt_name)
            or "You are a helpful coding assistant."
        )

        # Append live git context + project instruction files (P1 gap fixes)
        try:
            from .instruction_loader import build_runtime_context

            runtime_ctx = build_runtime_context(cwd=self.working_dir)
            if runtime_ctx:
                full_system_prompt = full_system_prompt + runtime_ctx
        except Exception:
            pass

        # Ensure the MessageManager contains the current system prompt (replace if different)
        try:
            self.msg_mgr.set_system_prompt(full_system_prompt)
        except Exception:
            pass

        initial_state = {
            "task": prompt,
            "session_id": self._current_task_id,
            "history": self.msg_mgr.messages,
            "verified_reads": list(self._session_read_files),
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "working_dir": str(self.working_dir),
            "system_prompt": full_system_prompt,
            "errors": [],
            # delegation tracking
            "delegations": [],
            "delegation_results": None,
            # planning fields
            "current_plan": [],
            "current_step": 0,
            # deterministic hints for nodes
            "deterministic": getattr(self, "deterministic", False),
            "cancel_event": cancel_event,
            "seed": getattr(self, "seed", None),
            # infinite loop prevention
            "empty_response_count": 0,
            # analysis phase
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            # VOL7-3: repo_summary_data generated by analysis_node; initialised
            # here so downstream nodes never encounter a missing key.
            "repo_summary_data": None,
            # debug retry tracking
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            # VOL7-1: total_debug_attempts was missing — the global cap in
            # should_after_evaluation (MAX_TOTAL_DEBUG=9) relies on this field.
            # Without initialisation the counter starts at None → coerces to 0
            # on the first debug cycle but may not persist cleanly across cycles.
            "total_debug_attempts": 0,
            # verification result
            "verification_passed": None,
            "verification_result": None,
            # step controller
            "step_controller_enabled": True,
            # task decomposition
            "task_decomposed": False,
            # tool call budgeting
            "tool_call_count": 0,
            "max_tool_calls": 30,
            # Tool cooldown tracker: keyed by "tool_name:path_arg", value = tool_call_count at last use
            "tool_last_used": {},
            # Fast read-before-edit lookup: resolved_path → True
            "files_read": {},
            # ME-1 fix: analyst_findings was missing from initial_state — analyst_delegation_node
            # writes this field and planning_node reads it; a missing initial value causes
            # a KeyError on the first planning pass when no delegation has run yet.
            "analyst_findings": None,
            # plan resumption flag written by planning_node
            "plan_resumed": False,
            # Plan Mode fields
            "plan_mode_enabled": getattr(
                getattr(self, "plan_mode", None), "enabled", False
            ),
            "awaiting_plan_approval": False,
            "plan_mode_approved": None,
            "plan_mode_blocked_tool": None,
            # PRSW: FileLockManager reference
            "_file_lock_manager": getattr(self, "file_lock_manager", None),
            # PRSW: Pending write operations (empty at task start)
            "_write_queue": [],
            # Phase B: P2P session tracking (singleton references, not serialised)
            "_agent_session_manager": getattr(self, "agent_session_manager", None),
            "_agent_messages": [],
            "_context_controller": getattr(self, "context_controller", None),
            # Phase 4: Token auto-compact tracking
            "last_compact_at": None,
            "last_compact_turn": 0,
            "context_degradation_detected": False,
            # P1-2/P1-3: Inner-loop counters
            "plan_attempts": 0,
            "replan_attempts": 0,
            # P1-6: Warnings are advisory only — enforce_warnings=True triggers infinite
            # replanning loops because any plan without an explicit test/verify step is
            # rejected. The plan_attempts guard (>=3 → force execution) was added as a
            # band-aid but the recursion limit is hit before it fires. Keep False.
            "plan_enforce_warnings": False,
            # Turn limit: independent of tool_call_count; bounded by max_turns
            "turn_count": 0,
            "max_turns": 50,
            "plan_strict_mode": False,
            # P3-1: Structured dependency data from analysis phase
            "call_graph": None,
            "test_map": None,
            # Doom loop detection: fingerprints of last N tool calls
            "recent_tool_calls": [],
            # Phase A: DAG execution fields (populated by planning_node)
            "plan_dag": None,
            "execution_waves": None,
            "current_wave": 0,
            # Phase 3: Preview Mode
            "preview_mode_enabled": False,
            "pending_preview_id": None,
            "awaiting_user_input": False,
            "preview_confirmed": None,
            # Token Auto-Compact
            "_should_distill": None,
            "_force_compact": None,
            "_budget_compaction": None,
            # P2P context buffering
            "_p2p_context": None,
            # Fields present in AgentState but omitted from prior initial_state versions;
            # explicit None/0/{} prevents KeyError in nodes that assume presence.
            "action_failed": None,
            "delegation_depth": 0,
            "evaluation_result": None,
            "last_debug_error_type": None,
            "last_tool_name": None,
            "no_plan_fail_count": 0,
            "original_task": None,
            "plan_progress": None,
            "plan_validation": None,
            "planned_action": None,
            "replan_required": None,
            "step_description": None,
            "step_retry_counts": {},
            "task_history": None,
            # S4-A: Snapshot tree-hash list — appended by perception_node each turn.
            "snapshots": [],
            # ORCH-W4: Current agent operating mode ("execution" or "planning").
            # Changed by plan_enter / plan_exit tool calls.
            "agent_mode": None,
            # SPAWN-W1: Parent session ID for delegated child sessions.  None = top-level.
            "parent_session_id": None,
        }

        # 2. Compile and Run Graph — P1 fix: use module-level cached graph so compilation
        # happens once per process instead of once per run_agent_once() call.
        graph = _get_compiled_graph()

        # TASK-12: max_turns guard — enforce before invoking the graph so runaway
        # tasks cannot exceed the configured turn budget regardless of graph state.
        _max_turns = int(initial_state.get("max_turns") or 50)
        try:
            from src.core.config_loader import get as _cfg_get

            _cfg_max = _cfg_get("max_turns")
            if _cfg_max is not None:
                _max_turns = int(_cfg_max)
        except Exception:
            pass
        _turn_count = int(initial_state.get("turn_count") or 0)
        if _turn_count >= _max_turns:
            guilogger.warning(
                f"run_agent_once: turn_count={_turn_count} >= max_turns={_max_turns} — refusing to start new turn"
            )
            return {
                "ok": False,
                "error": f"max_turns limit reached ({_turn_count}/{_max_turns}).",
                "assistant_message": (
                    f"Task aborted: the maximum number of turns ({_max_turns}) has been "
                    "reached. Start a new session to continue."
                ),
            }

        # Mint a fresh correlation ID for this agent turn so all EventBus events
        # and LLM call logs share the same trace token (#26).
        cid = new_correlation_id()
        guilogger.info(f"run_agent_once: starting with task: {prompt[:80]} [cid={cid}]")

        # Initialize final_state before try to satisfy LSP
        final_state: dict = {}

        try:
            # MED-5 fix: reuse the instance-level _graph_executor instead of
            # creating (and destroying) a new OS thread pool per run_agent_once() call.
            # The executor is created in __init__ and shut down in close().
            import concurrent.futures as _cf_pool

            _graph_executor = self._graph_executor

            # We use the same safe asyncio execution logic
            def _run_graph(state_to_run):
                # Run the langgraph for the provided state and return the resulting state
                return asyncio.run(
                    graph.ainvoke(
                        state_to_run,
                        {
                            "configurable": {"orchestrator": self},
                            "recursion_limit": 50,
                        },
                    )
                )

            try:
                # Allow multiple graph rounds to consume multi-turn tool sequences (bounded)
                max_rounds = 12
                current_state = initial_state
                for round_idx in range(max_rounds):
                    # Check for cancellation at the start of each round
                    if (
                        cancel_event
                        and hasattr(cancel_event, "is_set")
                        and cancel_event.is_set()
                    ):
                        guilogger.info(
                            "Orchestrator: Task canceled by user during round loop"
                        )
                        break

                    try:
                        asyncio.get_running_loop()
                        # Running loop detected — submit to the reused executor (P2 fix)
                        future = _graph_executor.submit(_run_graph, current_state)
                        next_state = future.result()
                    except RuntimeError:
                        next_state = _run_graph(current_state)

                    guilogger.info(
                        f"Graph round {round_idx}: next_state keys: {list(next_state.keys()) if next_state else 'None'}"
                    )

                    # If nothing changed (no new assistant turn) or no next action, stop early
                    final_state = next_state

                    # Determine last assistant content produced in this run
                    assistant_msgs = [
                        m["content"]
                        for m in final_state.get("history", [])
                        if m.get("role") == "assistant"
                    ]
                    last_assistant = assistant_msgs[-1] if assistant_msgs else ""

                    # Determine whether the assistant suggested a tool that still needs execution.
                    # If the assistant message contains a tool block but a 'tool' role entry with
                    # execution results exists after that assistant message, consider it handled.
                    try:
                        from src.core.orchestration.tool_parser import (
                            parse_tool_block as _parse_tool_block,
                        )

                        has_tool_block = (
                            True if _parse_tool_block(last_assistant) else False
                        )
                    except Exception:
                        has_tool_block = False

                    # Find index of last assistant message in the full history
                    history = final_state.get("history", [])
                    last_assistant_idx = None
                    for idx in range(len(history) - 1, -1, -1):
                        if (
                            history[idx].get("role") == "assistant"
                            and history[idx].get("content") == last_assistant
                        ):
                            last_assistant_idx = idx
                            break

                    handled = False
                    if last_assistant_idx is not None:
                        # Check if there's an execution result after this assistant msg.
                        # execution_node stores results with role="user" (not "tool"), so we
                        # match on content alone — any message containing "tool_execution_result"
                        # means the tool was already executed and we should stop looping.
                        for later in history[last_assistant_idx + 1 :]:
                            if "tool_execution_result" in (later.get("content") or ""):
                                handled = True
                                break

                    # If there's no unhandled tool block, we're done
                    if not has_tool_block or handled:
                        break

                    # Prepare next iteration: feed the graph with the new history and verified reads
                    _next_history = final_state.get("history", [])
                    # Guard: OpenAI-compatible APIs require the last message to be 'user'.
                    # If history ends with an assistant message (no tool result following),
                    # inject a bridging user message to prevent consecutive-assistant violations.
                    if _next_history and _next_history[-1].get("role") == "assistant":
                        _next_history = list(_next_history) + [
                            {
                                "role": "user",
                                "content": (
                                    "Continue. If the task is already complete, "
                                    "output STATUS: complete with no tool call."
                                ),
                            }
                        ]
                    # SCAN3-1 fix: preserve ALL state from the completed round and
                    # only override the fields that must reset for the next round.
                    # Prior hand-rolled reconstruction silently discarded wave state,
                    # analysis results, loop counters, delegation state, and more.
                    _prev_working_dir = current_state.get("working_dir")
                    _prev_system_prompt = current_state.get("system_prompt")
                    current_state = {
                        **final_state,
                        # Fields that must be explicitly reset / refreshed each round:
                        "history": _next_history,
                        "verified_reads": final_state.get("verified_reads")
                        or list(self._session_read_files),
                        "next_action": None,
                        "last_result": None,
                        "errors": [],
                        # Execution context fields — keep stable from initial invocation
                        "working_dir": _prev_working_dir
                        or final_state.get("working_dir"),
                        "system_prompt": _prev_system_prompt
                        or final_state.get("system_prompt"),
                        "deterministic": getattr(self, "deterministic", False),
                        "seed": getattr(self, "seed", None),
                        "cancel_event": cancel_event,
                        # Cooldown / read-tracking dicts must not be None
                        "tool_last_used": final_state.get("tool_last_used") or {},
                        "files_read": final_state.get("files_read") or {},
                        "step_retry_counts": final_state.get("step_retry_counts") or {},
                    }
            finally:
                # MED-5 fix: _graph_executor is now instance-level and shut down in
                # close() — do NOT shut it down here or subsequent calls will fail.
                pass

            # Check if we broke out due to cancellation
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                guilogger.info(
                    "Orchestrator: Task was canceled, returning cancel response"
                )
                return {
                    "assistant_message": "[yellow]⚠ Task canceled by user.[/yellow]",
                    "canceled": True,
                }

            # Debug: check why verified_reads might be empty
            if final_state:
                guilogger.info(
                    f"Final state verified reads: {final_state.get('verified_reads')}"
                )

            # 3. Synchronize MessageManager with graph history
            # The graph history contains new turns added by nodes via operator.add reducer
            if final_state and "history" in final_state:
                # Only append messages that are new since last sync
                msg_count_before = len(self.msg_mgr.messages)
                if len(final_state["history"]) > msg_count_before:
                    new_turns = final_state["history"][msg_count_before:]
                    for turn in new_turns:
                        self.msg_mgr.append(turn["role"], turn["content"])

            # Update session tracking
            if final_state:
                for path in final_state.get("verified_reads", []):
                    self._session_read_files.add(path)

            # Construct final response
            assistant_msgs = []
            tool_results = []
            last_tool_name = None
            if final_state and "history" in final_state:
                assistant_msgs = [
                    m["content"]
                    for m in final_state["history"]
                    if m["role"] == "assistant"
                ]
                # Extract tool results for display with tool name.
                # execution_node stores results with role="user", not "tool".
                # Accept either role as long as content contains "tool_execution_result".
                for i, m in enumerate(final_state["history"]):
                    is_tool_result = m.get("role") == "tool" or (
                        m.get("role") == "user"
                        and "tool_execution_result" in (m.get("content") or "")
                    )
                    if is_tool_result:
                        content = m.get("content", "")
                        # Try to find the tool name from preceding assistant message
                        tool_name = None
                        if i > 0:
                            prev_msg = final_state["history"][i - 1]
                            if prev_msg.get("role") == "assistant":
                                try:
                                    from src.core.orchestration.tool_parser import (
                                        parse_tool_block,
                                    )

                                    parsed = parse_tool_block(
                                        prev_msg.get("content", "")
                                    )
                                    if parsed and parsed.get("name"):
                                        tool_name = parsed["name"]
                                except Exception:
                                    pass

                        # Extract the result from tool_execution_result wrapper.
                        # execution_node wraps: {"tool_execution_result": {"ok": True, "result": {...}}}
                        # We want the inner "result" dict for formatting.
                        if "tool_execution_result" in content:
                            import json

                            try:
                                data = json.loads(content)
                                # Unwrap the outer "tool_execution_result" envelope first
                                if (
                                    isinstance(data, dict)
                                    and "tool_execution_result" in data
                                ):
                                    ter = data["tool_execution_result"]
                                    if isinstance(ter, dict) and "result" in ter:
                                        result = ter["result"]
                                    elif isinstance(ter, dict):
                                        result = ter
                                    else:
                                        result = ter
                                    tool_results.append(result)
                                    if tool_name:
                                        last_tool_name = tool_name
                                # Legacy flat format: {"result": {...}}
                                elif isinstance(data, dict) and "result" in data:
                                    tool_results.append(data["result"])
                                    if tool_name:
                                        last_tool_name = tool_name
                                elif isinstance(data, dict) and data.get("ok"):
                                    tool_results.append(data)
                                    if tool_name:
                                        last_tool_name = tool_name
                            except (json.JSONDecodeError, TypeError):
                                tool_results.append(content)
                        elif content:
                            tool_results.append(content)
            guilogger.info(
                f"Graph execution completed in {final_state.get('rounds', 0) if final_state else 0} rounds"
                if final_state
                else "Graph execution completed"
            )

            history = final_state.get("history", []) if final_state else []
            work_summary = _generate_work_summary(final_state, history)

            # Build assistant_message: prefer tool result over raw YAML tool call
            last_assistant = assistant_msgs[-1] if assistant_msgs else ""

            # If last assistant message is just a tool call and we have results, show formatted result.
            # YAML blocks may start with ```yaml or bare "name:" (no fences).
            # LM Studio/Qwen models prefix the block with <think>...</think> — strip it first.
            import re as _re

            _last_stripped = _re.sub(
                r"<think>.*?</think>", "", last_assistant, flags=_re.DOTALL
            ).strip()
            _is_tool_call_msg = (
                not last_assistant
                or _last_stripped.startswith("name:")
                or _last_stripped.startswith("```yaml")
                or _last_stripped.startswith("```\nname:")
                or (_last_stripped.startswith("```") and "name:" in _last_stripped)
            )
            if tool_results and _is_tool_call_msg:
                # Use enhanced formatting based on tool type
                assistant_message = ""

                # Format each tool result using the appropriate formatter
                for i, result in enumerate(tool_results):
                    # Determine which tool this result belongs to
                    tool_name = None
                    if i == len(tool_results) - 1 and last_tool_name:
                        tool_name = last_tool_name

                    formatted = _format_tool_result(result, tool_name)
                    if formatted:
                        if assistant_message and not assistant_message.endswith("\n"):
                            assistant_message += "\n"
                        assistant_message += formatted + "\n"

                assistant_message = assistant_message.strip()
            else:
                assistant_message = last_assistant

            # OE4: Surface delegation_results so callers can read subagent outputs.
            # Previously the delegation_node populated this field but it was never
            # included in the return dict (fire-and-forget). Now callers can access it.
            delegation_results = (
                final_state.get("delegation_results") if final_state else None
            )
            if delegation_results:
                guilogger.info(
                    f"run_agent_once: delegation_results keys={list(delegation_results.keys())}"
                )

            self.cost_tracker.flush(task_id=getattr(self, "_current_task_id", ""))
            self.flush_execution_trace()

            # SES-W2: Persist assistant response to SessionStore transcript.
            if assistant_message:
                try:
                    self.session_store.add_message(
                        session_id=getattr(self, "_current_task_id", "unknown"),
                        role="assistant",
                        content=assistant_message.strip(),
                    )
                except Exception:
                    pass

            return {
                "assistant_message": assistant_message.strip(),
                "work_summary": work_summary,
                "delegation_results": delegation_results or {},
            }
        except StopAsyncIteration:
            # This is expected when the graph finishes successfully.
            history = (
                final_state.get("history", []) if isinstance(final_state, dict) else []
            )
            # Add session modified files to final_state for work summary
            if isinstance(final_state, dict):
                final_state["_session_modified_files"] = list(
                    self._session_modified_files
                )
                # Publish session changes for sidebar
                self._publish_session_changes()
            work_summary = _generate_work_summary(final_state, history)
            self.cost_tracker.flush(task_id=getattr(self, "_current_task_id", ""))
            self.flush_execution_trace()
            return {
                "assistant_message": "Graph finished.",
                "work_summary": work_summary,
            }
        except Exception as e:
            guilogger.error(f"Graph execution failed: {e}")
            self.msg_mgr.append("user", f"Error during tool execution: {e}")
            # Fallback: attempt to call the LLM directly (synchronous) to produce an assistant message
            try:
                from src.core.inference.llm_manager import call_model

                # Determine provider/model from adapter
                provider_name = None
                model_name = None
                try:
                    if (
                        self._adapter
                        and hasattr(self._adapter, "provider")
                        and isinstance(self._adapter.provider, dict)
                    ):
                        provider_name = self._adapter.provider.get(
                            "name"
                        ) or self._adapter.provider.get("type")
                except Exception:
                    provider_name = None
                try:
                    if (
                        self._adapter
                        and hasattr(self._adapter, "models")
                        and isinstance(self._adapter.models, list)
                        and self._adapter.models
                    ):
                        model_name = self._adapter.models[0]
                    elif self._adapter and hasattr(self._adapter, "default_model"):
                        model_name = self._adapter.default_model
                except Exception:
                    model_name = None

                messages_for_model = [
                    {"role": "system", "content": full_system_prompt},
                    {"role": "user", "content": prompt},
                ]
                try:
                    resp = asyncio.run(
                        call_model(
                            messages_for_model,
                            provider=provider_name,
                            model=model_name,
                            stream=False,
                            format_json=False,
                        )
                    )
                except Exception:
                    resp = None

                content = ""
                if isinstance(resp, dict):
                    if resp.get("choices"):
                        ch = resp["choices"][0].get("message")
                    else:
                        ch = resp.get("message")
                    if isinstance(ch, str):
                        content = ch
                    elif isinstance(ch, dict):
                        content = ch.get("content") or ""

                # Append assistant message if available
                if content:
                    try:
                        self.msg_mgr.append("assistant", content)
                    except Exception:
                        pass

                self.cost_tracker.flush(task_id=getattr(self, "_current_task_id", ""))
                self.flush_execution_trace()
                return {
                    "assistant_message": content if content else "",
                    "error": "graph_failed",
                    "exception": str(e),
                }
            except Exception:
                self.cost_tracker.flush(task_id=getattr(self, "_current_task_id", ""))
                self.flush_execution_trace()
                return {"error": "graph_failed", "exception": str(e)}

    # ------------------------------------------------------------------
    # S6-B: Provider family → prompt partial selection
    # ------------------------------------------------------------------

    _PROVIDER_FAMILY_MAP: Dict[str, str] = {
        # Anthropic
        "anthropic": "anthropic",
        # OpenAI-compatible cloud
        "openai": "openai",
        "openrouter": "openai",
        "github_copilot": "openai",
        "copilot": "openai",
        # Local / self-hosted
        "ollama": "local",
        "lm_studio": "local",
        "lmstudio": "local",
        "local": "local",
        # Test mock
        "mock": "mock",
    }

    @classmethod
    def _map_provider_family(cls, raw_name: str) -> str:
        """Map a provider name/type string to a canonical family string.

        Matching is case-insensitive and checks for substring containment so
        that variant spellings (e.g. "lm-studio", "LMStudio") are captured.
        Returns ``"default"`` when no known family matches.
        """
        normalised = raw_name.lower().replace("-", "_").replace(" ", "_")
        # Exact lookup first (fast path)
        if normalised in cls._PROVIDER_FAMILY_MAP:
            return cls._PROVIDER_FAMILY_MAP[normalised]
        # Substring scan (handles composite names like "anthropic-vertex")
        for key, family in cls._PROVIDER_FAMILY_MAP.items():
            if key in normalised:
                return family
        return "default"

    def get_provider_capabilities(self) -> Dict[str, Any]:
        """Get provider capabilities including supports_native_tools and provider_family.

        S6-B: Enriched to return ``provider_family`` so that
        ``ContextBuilder._select_prompt_partial()`` can choose the right
        provider-specific prompt partial without any special-casing outside
        this method.

        Capability keys returned:
          - ``supports_native_tools`` (bool)
          - ``provider_family`` (str)  — one of "anthropic", "openai", "local",
            "mock", "default"
          - ``model`` (str | None)     — active model name if known
          - ``provider_name`` (str)    — raw provider name for debugging
        """
        capabilities: Dict[str, Any] = {
            "supports_native_tools": False,
            "provider_family": "default",
            "model": None,
            "provider_name": "",
        }
        try:
            if not self._adapter:
                return capabilities

            # ── 1. Extract raw name strings ────────────────────────────────
            raw_name: str = ""
            raw_type: str = ""

            provider_attr = getattr(self._adapter, "provider", None)
            if isinstance(provider_attr, dict):
                raw_name = str(provider_attr.get("name") or "")
                raw_type = str(provider_attr.get("type") or "")
                capabilities["supports_native_tools"] = bool(
                    provider_attr.get("supports_native_tools", False)
                )

            # Fall back to adapter.name (present on all OpenAICompatibleAdapter
            # subclasses and MockAdapter)
            if not raw_name:
                raw_name = str(getattr(self._adapter, "name", "") or "")

            # ── 2. Resolve provider_family ─────────────────────────────────
            # Prefer the explicit "type" field when non-empty; otherwise use name.
            lookup_str = raw_type if raw_type else raw_name
            family = self._map_provider_family(lookup_str)
            # If type didn't resolve, try name as well
            if family == "default" and raw_type and raw_name:
                family = self._map_provider_family(raw_name)

            capabilities["provider_family"] = family
            capabilities["provider_name"] = raw_name or raw_type

            # ── 3. Active model ────────────────────────────────────────────
            capabilities["model"] = getattr(self._adapter, "default_model", None)

        except Exception:
            pass
        return capabilities

    def close(self) -> None:
        """Release long-lived resources held by this orchestrator.

        Should be called when the orchestrator is no longer needed (e.g. when
        the TUI session ends or a subagent finishes).
        """
        # MED-5 fix: shut down the graph executor so its background thread is
        # cleanly joined and OS resources are released.
        try:
            if hasattr(self, "_graph_executor"):
                self._graph_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            if hasattr(self, "_tool_executor"):
                self._tool_executor.shutdown(wait=False)
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
