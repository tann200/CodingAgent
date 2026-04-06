"""tests/unit/test_snapshot_manager.py — Unit tests for S4-A GitSnapshotManager.

These tests use a real temporary git repository (created by `git init`) so
no mocking of subprocess calls is required.  The shadow git repo is placed in
a second temporary directory to keep things isolated.
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path

import pytest

from src.core.orchestration.snapshot_manager import (
    FileDiff,
    GitSnapshotManager,
    SnapshotPatch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args, cwd: Path) -> str:
    """Run a git command in *cwd* and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """An initialised git workspace with an initial commit."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git("init", cwd=ws)
    _git("config", "user.email", "test@test.com", cwd=ws)
    _git("config", "user.name", "Test", cwd=ws)
    # Create an initial file and commit so HEAD exists
    _write(ws / "hello.txt", "hello\n")
    _git("add", ".", cwd=ws)
    _git("commit", "-m", "init", cwd=ws)
    return ws


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture()
def mgr(workspace: Path, data_dir: Path) -> GitSnapshotManager:
    return GitSnapshotManager(
        workspace=workspace,
        project_id="test_project",
        data_dir=data_dir,
    )


# ---------------------------------------------------------------------------
# Data class tests
# ---------------------------------------------------------------------------


class TestSnapshotPatch:
    def test_default_files(self):
        p = SnapshotPatch(hash="abc")
        assert p.files == []

    def test_with_files(self):
        p = SnapshotPatch(hash="abc", files=["/foo/bar.py"])
        assert "/foo/bar.py" in p.files


class TestFileDiff:
    def test_defaults(self):
        d = FileDiff(file="a.py", before="old", after="new", additions=1, deletions=0)
        assert d.status == "modified"

    def test_custom_status(self):
        d = FileDiff(
            file="a.py", before="", after="x", additions=1, deletions=0, status="added"
        )
        assert d.status == "added"


# ---------------------------------------------------------------------------
# Disabled manager
# ---------------------------------------------------------------------------


class TestDisabledManager:
    @pytest.mark.asyncio
    async def test_track_returns_none(self, workspace: Path, data_dir: Path):
        mgr = GitSnapshotManager(
            workspace=workspace, project_id="p", data_dir=data_dir, enabled=False
        )
        result = await mgr.track()
        assert result is None

    @pytest.mark.asyncio
    async def test_patch_returns_empty(self, workspace: Path, data_dir: Path):
        mgr = GitSnapshotManager(
            workspace=workspace, project_id="p", data_dir=data_dir, enabled=False
        )
        patch = await mgr.patch("abc123")
        assert patch.hash == "abc123"
        assert patch.files == []

    @pytest.mark.asyncio
    async def test_restore_returns_false(self, workspace: Path, data_dir: Path):
        mgr = GitSnapshotManager(
            workspace=workspace, project_id="p", data_dir=data_dir, enabled=False
        )
        result = await mgr.restore("abc123")
        assert result is False

    @pytest.mark.asyncio
    async def test_diff_returns_empty_string(self, workspace: Path, data_dir: Path):
        mgr = GitSnapshotManager(
            workspace=workspace, project_id="p", data_dir=data_dir, enabled=False
        )
        result = await mgr.diff("abc123")
        assert result == ""

    @pytest.mark.asyncio
    async def test_diff_full_returns_empty(self, workspace: Path, data_dir: Path):
        mgr = GitSnapshotManager(
            workspace=workspace, project_id="p", data_dir=data_dir, enabled=False
        )
        result = await mgr.diff_full("abc", "def")
        assert result == []

    @pytest.mark.asyncio
    async def test_cleanup_is_noop(self, workspace: Path, data_dir: Path):
        mgr = GitSnapshotManager(
            workspace=workspace, project_id="p", data_dir=data_dir, enabled=False
        )
        await mgr.cleanup()  # should not raise


# ---------------------------------------------------------------------------
# Shadow repo initialisation
# ---------------------------------------------------------------------------


class TestShadowRepoInit:
    @pytest.mark.asyncio
    async def test_gitdir_created_on_first_track(self, mgr: GitSnapshotManager):
        assert not mgr._gitdir.exists()
        await mgr.track()
        assert mgr._gitdir.exists()

    @pytest.mark.asyncio
    async def test_second_track_does_not_reinit(self, mgr: GitSnapshotManager):
        await mgr.track()
        # Calling a second time should not fail or reinitialise
        hash2 = await mgr.track()
        assert hash2 is not None


# ---------------------------------------------------------------------------
# track()
# ---------------------------------------------------------------------------


class TestTrack:
    @pytest.mark.asyncio
    async def test_returns_40char_hash(self, mgr: GitSnapshotManager):
        hash_ = await mgr.track()
        assert hash_ is not None
        assert len(hash_) == 40

    @pytest.mark.asyncio
    async def test_different_hash_after_file_change(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        hash1 = await mgr.track()
        _write(workspace / "new_file.txt", "content")
        hash2 = await mgr.track()
        assert hash1 != hash2

    @pytest.mark.asyncio
    async def test_same_hash_when_no_change(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        hash1 = await mgr.track()
        hash2 = await mgr.track()
        assert hash1 == hash2


# ---------------------------------------------------------------------------
# patch()
# ---------------------------------------------------------------------------


class TestPatch:
    @pytest.mark.asyncio
    async def test_patch_no_changes(self, mgr: GitSnapshotManager, workspace: Path):
        hash_ = await mgr.track()
        assert hash_ is not None
        patch = await mgr.patch(hash_)
        assert patch.hash == hash_
        assert patch.files == []

    @pytest.mark.asyncio
    async def test_patch_detects_new_file(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        hash_ = await mgr.track()
        assert hash_ is not None
        _write(workspace / "added.txt", "new content\n")
        patch = await mgr.patch(hash_)
        assert any("added.txt" in f for f in patch.files)

    @pytest.mark.asyncio
    async def test_patch_detects_modified_file(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        hash_ = await mgr.track()
        assert hash_ is not None
        _write(workspace / "hello.txt", "changed content\n")
        patch = await mgr.patch(hash_)
        assert any("hello.txt" in f for f in patch.files)


# ---------------------------------------------------------------------------
# diff()
# ---------------------------------------------------------------------------


class TestDiff:
    @pytest.mark.asyncio
    async def test_diff_empty_when_no_changes(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        hash_ = await mgr.track()
        assert hash_ is not None
        d = await mgr.diff(hash_)
        assert d == ""

    @pytest.mark.asyncio
    async def test_diff_contains_added_lines(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        hash_ = await mgr.track()
        assert hash_ is not None
        _write(workspace / "hello.txt", "hello\nextra line\n")
        d = await mgr.diff(hash_)
        assert "+extra line" in d


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_reverts_file_change(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        hash_ = await mgr.track()
        assert hash_ is not None
        original = (workspace / "hello.txt").read_text()
        _write(workspace / "hello.txt", "completely different\n")
        ok = await mgr.restore(hash_)
        assert ok is True
        restored = (workspace / "hello.txt").read_text()
        assert restored == original

    @pytest.mark.asyncio
    async def test_restore_bad_hash_returns_false(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        await mgr.track()
        ok = await mgr.restore("0" * 40)
        assert ok is False


# ---------------------------------------------------------------------------
# revert()
# ---------------------------------------------------------------------------


class TestRevert:
    @pytest.mark.asyncio
    async def test_revert_patches_list(self, mgr: GitSnapshotManager, workspace: Path):
        hash_ = await mgr.track()
        assert hash_ is not None
        _write(workspace / "hello.txt", "modified\n")
        patch = await mgr.patch(hash_)
        await mgr.revert([patch])
        # File should be back to original
        assert (workspace / "hello.txt").read_text() == "hello\n"

    @pytest.mark.asyncio
    async def test_revert_empty_patches_is_noop(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        await mgr.track()
        await mgr.revert([])  # should not raise


# ---------------------------------------------------------------------------
# cleanup()
# ---------------------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_noop_when_gitdir_absent(
        self, workspace: Path, data_dir: Path
    ):
        mgr = GitSnapshotManager(workspace=workspace, project_id="p", data_dir=data_dir)
        await mgr.cleanup()  # should not raise even if shadow repo doesn't exist

    @pytest.mark.asyncio
    async def test_cleanup_after_track(self, mgr: GitSnapshotManager):
        await mgr.track()
        await mgr.cleanup()  # should complete without error


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_track_calls_do_not_raise(
        self, mgr: GitSnapshotManager, workspace: Path
    ):
        """Multiple concurrent track() calls should not corrupt state."""
        results = await asyncio.gather(
            mgr.track(),
            mgr.track(),
            mgr.track(),
        )
        # All results should be valid 40-char hashes
        for r in results:
            assert r is not None
            assert len(r) == 40
