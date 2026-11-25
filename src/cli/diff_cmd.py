"""``codingagent diff`` — show working-directory diff since the git snapshot.

Mirrors the TUI ``/diff`` command headlessly. Standalone CLI diff uses the plain
repo working-tree diff (``git diff HEAD``), optionally restricted to a path, and
gracefully handles unborn ``HEAD`` repositories via ``git diff --cached`` plus
untracked file inspection.
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

    has_head = _has_head_commit(workdir)
    base = "HEAD" if has_head else "unborn HEAD"
    diff_text = _git_diff(workdir, path, has_head=has_head)
    if not diff_text or not diff_text.strip():
        diff_text = f"(no changes since {base})"

    if as_json:
        print_json({"workdir": workdir, "base": base, "diff": diff_text})
        return 0

    print(f"Working-directory diff (base: {base}):")
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


def _has_head_commit(workdir: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _git_diff(workdir: str, path: Optional[str], *, has_head: Optional[bool] = None) -> str:
    if has_head is None:
        has_head = _has_head_commit(workdir)

    if has_head:
        cmd = ["git", "diff", "--stat", "--patch", "HEAD"]
        if path:
            cmd.extend(["--", path])
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

    # Unborn HEAD: inspect staged changes and untracked files
    sections: list[str] = []
    cached_cmd = ["git", "diff", "--cached", "--stat", "--patch"]
    if path:
        cached_cmd.extend(["--", path])
    try:
        cached = subprocess.run(
            cached_cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if cached.stdout and cached.stdout.strip():
            sections.append(cached.stdout.rstrip())
    except Exception:
        pass

    status_cmd = ["git", "status", "--porcelain"]
    if path:
        status_cmd.extend(["--", path])
    try:
        st = subprocess.run(
            status_cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        untracked = [
            line[3:]
            for line in (st.stdout or "").splitlines()
            if line.startswith("?? ")
        ]
        if untracked:
            sections.append(
                "Untracked files:\n" + "\n".join(f"  + {f}" for f in untracked)
            )
    except Exception:
        pass

    return "\n\n".join(sections)
