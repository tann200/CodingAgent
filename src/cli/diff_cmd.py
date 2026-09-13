"""``codingagent diff`` — show working-directory diff since the git snapshot.

Mirrors the TUI ``/diff`` command headlessly. Standalone CLI diff uses the plain
repo working-tree diff (``git diff HEAD``), optionally restricted to a path.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Optional

from src.cli._helpers import print_json, resolve_workdir


def run_diff(args: Any) -> int:
    workdir = resolve_workdir(getattr(args, "workdir", None))
    as_json = getattr(args, "json", False)
    path: Optional[str] = getattr(args, "path", None)

    is_repo = _is_git_repo(workdir)
    if not is_repo:
        payload = {"workdir": workdir, "repo": False, "diff": ""}
        if as_json:
            print_json(payload)
        else:
            print(f"Error: {workdir} is not a git repository", file=sys.stderr)
        return 1

    diff_text = _git_diff(workdir, path)
    if not diff_text or not diff_text.strip():
        diff_text = "(no changes since HEAD)"

    if as_json:
        print_json({"workdir": workdir, "base": "HEAD", "diff": diff_text})
        return 0

    print("Working-directory diff (base: HEAD):")
    print(diff_text)
    return 0


def _is_git_repo(workdir: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


def _git_diff(workdir: str, path: Optional[str]) -> str:
    cmd = ["git", "diff", "--stat", "--patch", "HEAD"]
    if path:
        cmd.append("--")
        cmd.append(path)
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return proc.stdout or ""
    except Exception:
        return ""