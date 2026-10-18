import os
from pathlib import Path

import pytest

from src.core.orchestration.workspace_guard import WorkspaceGuard
from src.tools._edit_tools import edit_by_line_range, edit_file, edit_file_atomic, multiedit
from src.tools._file_io import delete_file, rename_file, write_file


def test_workspace_guard_dot_segments(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    git_config = git_dir / "config"
    git_config.write_text("[core]\n")

    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()

    guard = WorkspaceGuard(workspace_root=tmp_path)

    assert guard.is_protected("sub/../.git") is True
    assert guard.is_protected("sub/../.git/config") is True
    assert guard.is_protected("./.git") is True
    assert guard.is_protected(".git") is True


def test_workspace_guard_symlink_bypass(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    git_config = git_dir / "config"
    git_config.write_text("[core]\n")

    symlink_to_git = tmp_path / "link_to_git"
    symlink_to_git.symlink_to(git_dir)

    guard = WorkspaceGuard(workspace_root=tmp_path)
    assert guard.is_protected("link_to_git") is True

    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=1")
    symlink_to_env = tmp_path / "link_to_env"
    symlink_to_env.symlink_to(env_file)

    assert guard.is_protected("link_to_env") is True
    assert guard.is_secret("link_to_env") is True


def test_delete_file_blocks_workspace_root_wipe(tmp_path: Path):
    # Attempting to delete "." or "sub/.." must fail and not delete root
    res = delete_file(path=".", workdir=tmp_path)
    assert res.get("status") == "error"
    assert "Cannot delete the workspace root directory" in res.get("error", "")

    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()
    res2 = delete_file(path="sub/..", workdir=tmp_path)
    assert res2.get("status") == "error"
    assert "Cannot delete the workspace root directory" in res2.get("error", "")

    assert tmp_path.exists()
    assert sub_dir.exists()


def test_delete_file_blocks_dot_segment_git_deletion(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()

    res = delete_file(path="sub/../.git", workdir=tmp_path)
    assert res.get("status") == "error"
    assert git_dir.exists()


def test_mutating_tools_block_symlink_bypasses(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=1")

    symlink_to_env = tmp_path / "link_to_env"
    symlink_to_env.symlink_to(env_file)

    # 1. write_file on symlink pointing to .env
    res_w = write_file("link_to_env", "NEW_SECRET=2", workdir=tmp_path)
    assert res_w.get("status") == "error"
    assert "Protected" in res_w.get("error", "")

    # 2. edit_file on symlink
    res_e = edit_file("link_to_env", "--- a/link_to_env\n+++ b/link_to_env\n", workdir=tmp_path)
    assert res_e.get("status") == "error"
    assert "Protected" in res_e.get("error", "")

    # 3. edit_by_line_range on symlink
    res_lr = edit_by_line_range("link_to_env", 1, 1, "NEW=3", workdir=tmp_path)
    assert res_lr.get("status") == "error"
    assert "Protected" in res_lr.get("error", "")

    # 4. edit_file_atomic on symlink
    res_at = edit_file_atomic("link_to_env", "SECRET=1", "NEW=4", workdir=tmp_path)
    assert res_at.get("status") == "error"
    assert "Protected" in res_at.get("error", "")

    # 5. multiedit on symlink
    res_me = multiedit("link_to_env", [{"old_text": "SECRET=1", "new_text": "NEW=5"}], workdir=tmp_path)
    assert res_me.get("status") == "error"
    assert "Protected" in res_me.get("error", "")

    # 6. rename_file on symlink
    res_rn = rename_file(path="link_to_env", new_path="renamed.txt", workdir=tmp_path)
    assert res_rn.get("status") == "error"
    assert "Protected" in res_rn.get("error", "")

    # 7. delete_file on symlink pointing to .env
    res_del = delete_file(path="link_to_env", workdir=tmp_path)
    assert res_del.get("status") == "error"
    assert "Protected" in res_del.get("error", "")
