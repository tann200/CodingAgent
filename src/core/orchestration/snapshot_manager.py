"""snapshot_manager.py — Git-backed workspace snapshot system (S4-A).

Mirrors OpenCode's packages/opencode/src/snapshot/index.ts, adapted to Python
and our async subprocess pattern.

Design
------
Snapshots are stored in a **shadow git repository** whose ``--git-dir`` lives
under the CodingAgent data directory (use ``src.core.paths.get_snapshots_dir()``,
e.g. get_data_dir()/snapshots/<project_id>/).
The ``--work-tree`` is the actual workspace root.  This means:

- The user's own .git repo is never touched.
- Every call to ``track()`` creates a tree object (not a commit) — cheap O(1)
  for index-only changes.
- ``restore(hash)`` uses ``read-tree`` + ``checkout-index`` to roll back.
- ``diff(hash)`` produces a unified diff against the current working tree.
- ``revert(patches)`` applies per-file rollback using ``git checkout <hash> -- <file>``.

File-size limit: files > 2 MB are excluded from snapshots via the shadow repo's
``info/exclude``.  Binary files are handled gracefully.

Thread / async safety
---------------------
An ``asyncio.Lock`` serialises all git operations per instance so concurrent
tool calls cannot corrupt the index.

Usage::

    from pathlib import Path
    from src.core.orchestration.snapshot_manager import GitSnapshotManager

    mgr = GitSnapshotManager(workspace=Path("/path/to/project"), project_id="myproj")
    hash_ = await mgr.track()          # snapshot current state → returns tree hash
    # … agent makes changes …
    diff  = await mgr.diff(hash_)      # unified diff since snapshot
    patch = await mgr.patch(hash_)     # SnapshotPatch(hash, files=[...])
    await mgr.restore(hash_)           # roll back all files
    await mgr.cleanup()                # prune old objects (call periodically)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from src.core.paths import get_snapshots_dir

logger = logging.getLogger(__name__)

# Files larger than this are excluded from snapshots (mirrors OpenCode's 2 MB limit)
_SIZE_LIMIT_BYTES: int = 2 * 1024 * 1024
_PRUNE_EXPIRE: str = "7.days.ago"
# Maximum time (seconds) allowed for any single git subprocess call
_GIT_TIMEOUT: float = 60.0
# Only alphanumerics, hyphens, underscores, and dots are allowed in project IDs
_PROJECT_ID_RE: re.Pattern = re.compile(r"[^a-zA-Z0-9_.\-]")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SnapshotPatch:
    """Set of files that changed between a snapshot hash and the current tree."""

    hash: str
    files: List[str] = field(default_factory=list)


@dataclass
class FileDiff:
    """Per-file diff between two snapshot hashes."""

    file: str
    before: str
    after: str
    additions: int
    deletions: int
    status: str = "modified"  # "added" | "deleted" | "modified"


# ---------------------------------------------------------------------------
# Internal result type
# ---------------------------------------------------------------------------


@dataclass
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


# ---------------------------------------------------------------------------
# GitSnapshotManager
# ---------------------------------------------------------------------------


class GitSnapshotManager:
    """Manages a shadow git repo for workspace snapshots.

    Parameters
    ----------
    workspace:
        Absolute path to the workspace root (the ``--work-tree``).
    project_id:
        Stable identifier for this project — used to namespace the shadow
    repo under ``get_data_dir()/snapshots/<project_id>/``.
        data_dir:
        Override for the base data directory (default: ``get_data_dir()``).
        Useful in tests.
    enabled:
        Set to ``False`` to make all operations no-ops (useful for testing
        against repos where git is unavailable).
    """

    def __init__(
        self,
        workspace: Path,
        project_id: str,
        data_dir: Optional[Path] = None,
        enabled: bool = True,
    ) -> None:
        self.workspace = workspace.resolve()
        # Sanitise project_id: strip any characters that could escape the
        # snapshots directory (path traversal) or cause git to misbehave.
        safe_id = _PROJECT_ID_RE.sub("_", project_id)
        if not safe_id:
            safe_id = "default"
        self.project_id = safe_id
        self._enabled = enabled
        base = (data_dir or get_snapshots_dir()).resolve()
        # Shadow git repo lives outside the workspace
        self._gitdir: Path = base / "snapshots" / self.project_id
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def track(self) -> Optional[str]:
        """Stage all workspace changes and return a tree hash.

        Returns the git tree hash string, or ``None`` if snapshots are
        disabled or if git is unavailable.
        """
        if not self._enabled:
            return None
        async with self._lock:
            await self._ensure_init()
            await self._stage_files()
            result = await self._git(["write-tree"])
            if result.returncode != 0:
                logger.warning(
                    "snapshot.track: write-tree failed: %s", result.stderr.strip()
                )
                return None
            hash_ = result.stdout.strip()
            logger.info("snapshot.track: %s (cwd=%s)", hash_, self.workspace)
            return hash_

    async def patch(self, hash_: str) -> SnapshotPatch:
        """Return the set of files that changed between *hash_* and now."""
        if not self._enabled:
            return SnapshotPatch(hash=hash_)
        async with self._lock:
            await self._stage_files()
            result = await self._git(
                ["diff", "--cached", "--no-ext-diff", "--name-only", hash_, "--", "."]
            )
            if result.returncode != 0:
                logger.warning("snapshot.patch: diff failed: %s", result.stderr.strip())
                return SnapshotPatch(hash=hash_)
            files = [
                str(self.workspace / f)
                for f in result.stdout.strip().splitlines()
                if f.strip()
            ]
            return SnapshotPatch(hash=hash_, files=files)

    async def restore(self, hash_: str) -> bool:
        """Restore workspace to the state captured in *hash_*.

        Returns ``True`` on success, ``False`` on failure.
        """
        if not self._enabled:
            return False
        async with self._lock:
            result = await self._git(["read-tree", hash_])
            if result.returncode != 0:
                logger.error(
                    "snapshot.restore: read-tree failed for %s: %s",
                    hash_,
                    result.stderr.strip(),
                )
                return False
            checkout = await self._git(["checkout-index", "-a", "-f"])
            if checkout.returncode != 0:
                logger.error(
                    "snapshot.restore: checkout-index failed: %s",
                    checkout.stderr.strip(),
                )
                return False
            # Remove any files that were added after the snapshot was taken
            # (untracked in the shadow index) so the workspace matches exactly.
            clean = await self._git(["clean", "-fd"])
            if clean.returncode != 0:
                logger.warning(
                    "snapshot.restore: git clean -fd failed (non-fatal): %s",
                    clean.stderr.strip(),
                )
            logger.info("snapshot.restore: restored to %s", hash_)
            return True

    async def revert(self, patches: List[SnapshotPatch]) -> None:
        """Revert individual files to their snapshot states.

        For each unique file in *patches* (deduplicated across patches,
        first-wins), roll back to the hash in that patch.
        """
        if not self._enabled:
            return
        async with self._lock:
            seen: set = set()
            for patch in patches:
                for file_path in patch.files:
                    if file_path in seen:
                        continue
                    seen.add(file_path)
                    rel = os.path.relpath(file_path, self.workspace)
                    result = await self._git(["checkout", patch.hash, "--", rel])
                    if result.returncode != 0:
                        # Check whether the file existed in that snapshot
                        tree_check = await self._git(["ls-tree", patch.hash, "--", rel])
                        if tree_check.returncode == 0 and tree_check.stdout.strip():
                            logger.info(
                                "snapshot.revert: checkout failed but file in tree, keeping: %s",
                                file_path,
                            )
                        else:
                            # File did not exist at that snapshot — delete it
                            try:
                                Path(file_path).unlink(missing_ok=True)
                                logger.info(
                                    "snapshot.revert: deleted %s (not in snapshot)",
                                    file_path,
                                )
                            except OSError as exc:
                                logger.warning(
                                    "snapshot.revert: could not delete %s: %s",
                                    file_path,
                                    exc,
                                )

    async def diff(self, hash_: str) -> str:
        """Return a unified diff between *hash_* and the current working tree."""
        if not self._enabled:
            return ""
        async with self._lock:
            await self._stage_files()
            result = await self._git(
                ["diff", "--cached", "--no-ext-diff", hash_, "--", "."]
            )
            if result.returncode != 0:
                logger.warning("snapshot.diff: failed: %s", result.stderr.strip())
                return ""
            return result.stdout.strip()

    async def diff_full(self, from_hash: str, to_hash: str) -> List[FileDiff]:
        """Return per-file diffs between two snapshot hashes."""
        if not self._enabled:
            return []
        async with self._lock:
            diffs: List[FileDiff] = []

            # Get status map (A/D/M per file)
            status_result = await self._git(
                [
                    "diff",
                    "--no-ext-diff",
                    "--name-status",
                    "--no-renames",
                    from_hash,
                    to_hash,
                    "--",
                    ".",
                ]
            )
            status_map: Dict[str, str] = {}
            for line in status_result.stdout.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    code, fpath = parts
                    if code.startswith("A"):
                        status_map[fpath] = "added"
                    elif code.startswith("D"):
                        status_map[fpath] = "deleted"
                    else:
                        status_map[fpath] = "modified"

            # Get numstat for additions/deletions counts + per-file content
            numstat_result = await self._git(
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-renames",
                    "--numstat",
                    from_hash,
                    to_hash,
                    "--",
                    ".",
                ]
            )
            for line in numstat_result.stdout.strip().splitlines():
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                adds_str, dels_str, fpath = parts
                binary = adds_str == "-" and dels_str == "-"
                if binary:
                    before, after = "", ""
                    additions, deletions = 0, 0
                else:
                    try:
                        additions = int(adds_str)
                        deletions = int(dels_str)
                    except ValueError:
                        additions, deletions = 0, 0
                    before_r = await self._git(["show", f"{from_hash}:{fpath}"])
                    after_r = await self._git(["show", f"{to_hash}:{fpath}"])
                    before = before_r.stdout if before_r.returncode == 0 else ""
                    after = after_r.stdout if after_r.returncode == 0 else ""

                diffs.append(
                    FileDiff(
                        file=fpath,
                        before=before,
                        after=after,
                        additions=additions,
                        deletions=deletions,
                        status=status_map.get(fpath, "modified"),
                    )
                )
            return diffs

    async def cleanup(self) -> None:
        """Prune old objects from the shadow repo (call periodically)."""
        if not self._enabled:
            return
        if not self._gitdir.exists():
            return
        async with self._lock:
            result = await self._git(["gc", f"--prune={_PRUNE_EXPIRE}"])
            if result.returncode != 0:
                logger.warning("snapshot.cleanup: gc failed: %s", result.stderr.strip())
            else:
                logger.info("snapshot.cleanup: gc complete (prune=%s)", _PRUNE_EXPIRE)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_init(self) -> None:
        """Initialise the shadow git repo if it does not exist yet."""
        if self._gitdir.exists():
            return
        self._gitdir.mkdir(parents=True, exist_ok=True)
        await self._run_git(
            [
                "git",
                "--git-dir",
                str(self._gitdir),
                "--work-tree",
                str(self.workspace),
                "init",
            ],
        )
        # Configure the shadow repo
        for key, value in [
            ("core.autocrlf", "false"),
            ("core.longpaths", "true"),
            ("core.symlinks", "true"),
            ("core.fsmonitor", "false"),
        ]:
            await self._git(["config", key, value])
        logger.info("snapshot: initialised shadow repo at %s", self._gitdir)

    async def _stage_files(self) -> None:
        """Stage all workspace changes into the shadow repo index.

        Files > _SIZE_LIMIT_BYTES are excluded via info/exclude.
        """
        # Build exclusion list for large files
        large: List[str] = []
        try:
            for entry in self.workspace.rglob("*"):
                if entry.is_file():
                    try:
                        if entry.stat().st_size > _SIZE_LIMIT_BYTES:
                            rel = entry.relative_to(self.workspace)
                            large.append(str(rel))
                    except OSError:
                        pass
        except Exception:
            pass

        # Write info/exclude
        info_dir = self._gitdir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_lines = [f"/{f.replace(os.sep, '/')}" for f in large]
        (info_dir / "exclude").write_text(
            "\n".join(exclude_lines) + ("\n" if exclude_lines else "")
        )

        # Stage all changes
        # MED-15 fix: --sparse is not a valid flag for git-add; it caused
        # "unknown switch `s'" errors on stock git.  Use plain 'git add .'
        result = await self._git(["add", "."])
        if result.returncode != 0:
            logger.warning("snapshot: git add failed: %s", result.stderr.strip())

    async def _git(self, args: List[str]) -> _GitResult:
        """Run git with our shadow --git-dir and --work-tree flags."""
        cmd = [
            "git",
            "--git-dir",
            str(self._gitdir),
            "--work-tree",
            str(self.workspace),
            *args,
        ]
        return await self._run_git(cmd)

    async def _run_git(
        self,
        cmd: List[str],
        env: Optional[dict] = None,
    ) -> _GitResult:
        """Run *cmd* as a subprocess, capturing stdout/stderr.

        A hard ``_GIT_TIMEOUT`` second timeout is enforced; if exceeded the
        process is killed and an error _GitResult is returned.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workspace),
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=_GIT_TIMEOUT
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            logger.warning(
                "snapshot._run_git: timed out after %.0fs: %s", _GIT_TIMEOUT, cmd
            )
            return _GitResult(
                returncode=1, stdout="", stderr="git subprocess timed out"
            )
        return _GitResult(
            returncode=proc.returncode or 0,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
        )
