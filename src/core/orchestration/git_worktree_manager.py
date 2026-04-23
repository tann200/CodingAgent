"""
GAP-WORKTREE-1: Git worktree isolation for agent tasks.

Each task gets its own git worktree in a temp directory so the agent can
make changes in isolation without polluting the main working tree.
Worktrees are created from the current HEAD and removed on task completion.

Usage:
    mgr = GitWorktreeManager(workspace=Path("/my/repo"))
    path = await mgr.create(task_id="task_abc123")
    # ... run agent in path ...
    await mgr.remove(task_id="task_abc123")
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_WORKTREE_PREFIX = "agent-wt-"


class GitWorktreeManager:
    """Create and remove per-task git worktrees for isolated agent execution."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        # task_id → worktree path
        self._active: Dict[str, Path] = {}

    @property
    def active(self) -> Dict[str, Path]:
        """Read-only view of active task_id → worktree path mappings."""
        return dict(self._active)

    async def create(self, task_id: str, branch: Optional[str] = None) -> Path:
        """
        Create a new git worktree for *task_id*.

        If *branch* is None a detached HEAD worktree is created from the
        current HEAD commit.  If the task already has a worktree the existing
        path is returned without creating a new one.
        """
        if task_id in self._active:
            return self._active[task_id]

        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)
        wt_dir = Path(tempfile.mkdtemp(prefix=f"{_WORKTREE_PREFIX}{safe_id[:20]}-"))

        try:
            if branch:
                cmd = ["git", "worktree", "add", str(wt_dir), branch]
            else:
                cmd = ["git", "worktree", "add", "--detach", str(wt_dir)]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                shutil.rmtree(wt_dir, ignore_errors=True)
                raise RuntimeError(
                    f"git worktree add failed (rc={proc.returncode}): "
                    f"{stderr.decode(errors='replace').strip()}"
                )
        except Exception:
            shutil.rmtree(wt_dir, ignore_errors=True)
            raise

        self._active[task_id] = wt_dir
        logger.info(f"Created worktree {wt_dir} for task {task_id}")
        return wt_dir

    async def remove(self, task_id: str) -> bool:
        """
        Remove the worktree for *task_id*.

        Returns True if a worktree was removed, False if none existed.
        """
        wt_path = self._active.pop(task_id, None)
        if wt_path is None:
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "worktree",
                "remove",
                "--force",
                str(wt_path),
                cwd=self._workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    f"git worktree remove failed for {wt_path} "
                    f"(rc={proc.returncode}): {stderr.decode(errors='replace').strip()}"
                )
                # Best-effort filesystem cleanup even if git command failed
                shutil.rmtree(wt_path, ignore_errors=True)
        except Exception as exc:
            logger.warning(f"git worktree remove raised: {exc}; cleaning up manually")
            shutil.rmtree(wt_path, ignore_errors=True)

        logger.info(f"Removed worktree {wt_path} for task {task_id}")
        return True

    async def remove_all(self) -> None:
        """Remove all active worktrees — call on agent shutdown."""
        for task_id in list(self._active.keys()):
            await self.remove(task_id)

    async def list_registered(self) -> list[dict]:
        """
        Return the output of `git worktree list --porcelain` as parsed dicts.

        Each dict has keys: ``worktree``, ``HEAD``, ``branch`` (or ``detached=True``).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "worktree",
                "list",
                "--porcelain",
                cwd=self._workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return _parse_worktree_list(stdout.decode(errors="replace"))
        except Exception as exc:
            logger.warning(f"git worktree list failed: {exc}")
            return []


def _parse_worktree_list(text: str) -> list[dict]:
    """Parse ``git worktree list --porcelain`` output into a list of dicts."""
    entries: list[dict] = []
    current: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree"] = line[len("worktree ") :]
        elif line.startswith("HEAD "):
            current["HEAD"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :]
        elif line == "detached":
            current["detached"] = True
        elif line == "bare":
            current["bare"] = True
    if current:
        entries.append(current)
    return entries
