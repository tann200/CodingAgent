"""
Git integration tools for the coding agent.

Provides read and write git operations so the agent can inspect history,
commit work, stash changes, and restore files — the same primitives used
by Claude Code, OpenCode, and Copilot (F19 fix).
"""

from __future__ import annotations

import subprocess
import logging
from pathlib import Path
from typing import Dict, Any, Literal, Optional

from src.tools._tool import tool, PermissionKind

logger = logging.getLogger(__name__)

# Resolves lazily to the process working directory at the time it is dereferenced.
# It is used as a parameter default for public tool functions; callers that need
# a specific directory should pass an explicit Path.
DEFAULT_WORKDIR = Path(".")


def _run_git(args: list[str], workdir: Path, timeout: int = 30) -> Dict[str, Any]:
    """Run a git command and return a normalised result dict."""
    try:
        from src.tools.sandbox import run_sandboxed

        proc = run_sandboxed(
            ["git"] + args,
            cwd=Path(workdir),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            network=False,
        )
        if proc.returncode == 0:
            return {"status": "ok", "output": proc.stdout.strip()}
        return {
            "status": "error",
            "error": (proc.stderr.strip() or proc.stdout.strip()),
            "returncode": proc.returncode,
        }
    except FileNotFoundError:
        return {"status": "error", "error": "git not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"git command timed out after {timeout}s"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding"], permission_kind=PermissionKind.GIT_READ)
def git_read(
    action: Literal["status", "diff", "log"] = "status",
    staged: bool = False,
    path: Optional[str] = None,
    max_count: int = 10,
    workdir: Path = DEFAULT_WORKDIR,
) -> Dict[str, Any]:
    """Read-only git inspection (`status`, `diff`, or `log`).

    Args:
        action: Operation to run: ``status``, ``diff``, or ``log``.
        staged: For ``diff``, show staged index changes (`--cached`).
        path: For ``diff``, limit output to a specific path.
        max_count: For ``log``, maximum commits to return (default 10).
    """
    act = (action or "").strip().lower()
    if act == "status":
        return git_status(workdir=workdir)
    if act == "diff":
        return git_diff(workdir=workdir, staged=staged, path=path)
    if act == "log":
        return git_log(workdir=workdir, max_count=max_count)
    return {
        "status": "error",
        "error": f"Unknown git_read action {action!r}. Expected 'status', 'diff', or 'log'.",
    }


@tool(tags=["coding"], permission_kind=PermissionKind.GIT_READ)
def git_status(workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """
    Return the working-tree status in short format.

    Output mirrors `git status --short` — each line is `XY filename` where
    X = index status, Y = working-tree status (space = unmodified, M = modified,
    A = added, D = deleted, ? = untracked).
    """
    result = _run_git(["status", "--short", "--branch"], workdir)
    if result["status"] == "ok":
        lines = result["output"].splitlines()
        branch_line = lines[0] if lines else ""
        file_lines = lines[1:] if len(lines) > 1 else []
        return {
            "status": "ok",
            "branch": branch_line.lstrip("## "),
            "files": file_lines,
            "output": result["output"],
        }
    return result


@tool(tags=["coding"], permission_kind=PermissionKind.GIT_READ)
def git_log(
    workdir: Path = DEFAULT_WORKDIR,
    max_count: int = 10,
) -> Dict[str, Any]:
    """
    Return the last `max_count` commits in one-line format.

    Each entry is `<hash> <subject>`.
    """
    result = _run_git(
        ["log", f"--max-count={max_count}", "--oneline", "--no-decorate"],
        workdir,
    )
    if result["status"] == "ok":
        commits = [ln for ln in result["output"].splitlines() if ln.strip()]
        return {"status": "ok", "commits": commits}
    return result


@tool(tags=["coding"], permission_kind=PermissionKind.GIT_READ)
def git_diff(
    workdir: Path = DEFAULT_WORKDIR,
    staged: bool = False,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return unified diff of working-tree (or staged) changes.

    Args:
        staged: If True, show staged (index) diff (`git diff --cached`).
        path: Limit diff to this path (optional).
    """
    from src.core.security.redaction import is_secret_path, redact_secrets

    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        if is_secret_path(Path(path)):
            return {
                "status": "error",
                "error": f"Access denied: '{path}' is a protected secret file.",
            }
        args += ["--", path]
    result = _run_git(args, workdir)
    if result["status"] == "ok":
        return {"status": "ok", "diff": redact_secrets(result["output"])}
    return result


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.GIT_WRITE)
def git_commit(
    message: str,
    workdir: Path = DEFAULT_WORKDIR,
    add_all: bool = False,
) -> Dict[str, Any]:
    """
    Stage changes and create a commit with the given message.

    Args:
        message: Commit message (required, must be non-empty).
        add_all: If True, run `git add -A` before committing. Default is False
                 to prevent accidental mass-commits; explicitly specify files
                 to stage or use git add for specific files.
    """
    if not message or not message.strip():
        return {"status": "error", "error": "Commit message must not be empty."}

    if add_all:
        add_result = _run_git(["add", "-A"], workdir)
        if add_result["status"] != "ok":
            return add_result

    # D-04: Idempotency guard — abort early if the tree is already clean
    status_result = _run_git(["status", "--porcelain"], workdir)
    if status_result["status"] == "ok" and not status_result["output"].strip():
        return {
            "status": "no_change",
            "output": "Nothing to commit, working tree clean.",
        }

    result = _run_git(["commit", "-m", message.strip()], workdir)
    if result["status"] == "ok":
        # Extract the commit hash from output like "[main abc1234] message"
        first_line = result["output"].splitlines()[0] if result["output"] else ""
        return {"status": "ok", "output": result["output"], "summary": first_line}
    return result


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.GIT_WRITE)
def git_stash(
    workdir: Path = DEFAULT_WORKDIR,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Stash all local modifications.

    Args:
        message: Optional stash description.
    """
    args = ["stash", "push", "--include-untracked"]
    if message:
        args += ["-m", message]
    return _run_git(args, workdir)


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.GIT_WRITE)
def git_restore(
    path: str,
    workdir: Path = DEFAULT_WORKDIR,
    staged: bool = False,
) -> Dict[str, Any]:
    """
    Discard working-tree changes to a file (`git restore`).

    Args:
        path: File path relative to workdir.
        staged: If True, unstage the file (`git restore --staged`).
    """
    if not path or not path.strip():
        return {"status": "error", "error": "path must not be empty."}
    from src.tools._file_io import _pre_write_check

    try:
        checked_path, pre_err = _pre_write_check(
            path,
            Path(workdir),
            operation="git_restore",
            check_binary=False,
            check_read=False,
        )
    except PermissionError as exc:
        return {"status": "error", "error": str(exc)}
    if pre_err is not None or checked_path is None:
        return pre_err or {"status": "error", "error": "Pre-write check failed"}
    args = ["restore"]
    if staged:
        args.append("--staged")
    args.extend(["--", path])
    result = _run_git(args, workdir)
    if result["status"] == "ok":
        return {"status": "ok", "path": path, "output": result.get("output", "")}
    return result
