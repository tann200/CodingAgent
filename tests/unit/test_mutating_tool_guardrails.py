"""Unit tests for SPEC-08: Mutating tool guardrails, RBW, permissions, and directory rollback."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

from src.core.orchestration.orchestrator import Orchestrator
from src.core.orchestration.rollback_manager import RollbackManager
from src.tools._edit_tools import multiedit
from src.tools._file_io import delete_file, read_file
from src.tools.ast_tools import ast_rename
from src.tools.constants import DRY_RUN_BLOCKED_TOOLS, WRITE_TOOLS_REQUIRING_READ
from src.tools.guardrails import reset_guardrail_state
from src.tools.patch_tools import edit_code_block
from src.tools.rollback_tools import revert_last_tool
from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR
from src.tools.tools_config import (
    PermissionLevel,
    TOOL_PERMISSIONS,
    get_tool_permission,
)


@pytest.fixture(autouse=True)
def clean_guardrails():
    reset_guardrail_state()
    yield
    reset_guardrail_state()


def test_constants_membership():
    """Verify that all mutating tools are in WRITE_TOOLS_REQUIRING_READ and DRY_RUN_BLOCKED_TOOLS."""
    required_write = {
        "multiedit",
        "edit_code_block",
        "ast_rename",
        "lsp_rename",
        "format_file",
    }
    assert required_write <= WRITE_TOOLS_REQUIRING_READ

    required_dry_run = {
        "multiedit",
        "edit_code_block",
        "ast_rename",
        "lsp_rename",
        "format_file",
        "create_directory",
        "git_stash",
        "git_restore",
        "run_tests",
        "run_js_tests",
        "delegate_task",
        "delegate_tasks_parallel",
    }
    assert required_dry_run <= DRY_RUN_BLOCKED_TOOLS


def test_tool_permissions_table():
    """Verify read-only tools, prompt tools, and danger tools in TOOL_PERMISSIONS."""
    read_only_tools = [
        "read_file_chunk",
        "read_file_bytes",
        "batched_file_read",
        "tail_log_file",
        "repo_overview",
        "find_files",
        "ast_list_symbols",
        "lsp_diagnostics",
        "lsp_references",
        "lsp_definition",
        "lsp_symbols",
        "lsp_hover",
        "diff_state",
        "list_snapshots",
        "list_checkpoints",
    ]
    for tool_name in read_only_tools:
        assert (
            get_tool_permission(tool_name) == PermissionLevel.READ_ONLY
        ), f"{tool_name} should be READ_ONLY"

    prompt_tools = ["delegate_tasks_parallel", "lsp_rename", "ast_rename"]
    for tool_name in prompt_tools:
        assert (
            get_tool_permission(tool_name) == PermissionLevel.PROMPT
        ), f"{tool_name} should be PROMPT"

    danger_tools = ["run_js_tests", "run_tests_legacy"]
    for tool_name in danger_tools:
        assert (
            get_tool_permission(tool_name) == PermissionLevel.DANGER
        ), f"{tool_name} should be DANGER"


def test_rbw_with_cwd_divergence(tmp_path: Path):
    """When cwd != workspace, multiedit and edit_code_block must reject unread files in workspace."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    test_file = workspace_dir / "target.py"
    test_file.write_text("x = 10\ny = 20\n", encoding="utf-8")

    orig_cwd = os.getcwd()
    os.chdir(other_dir)
    try:
        # multiedit without reading must fail RBW
        res_multi = multiedit(
            path="target.py",
            edits=[{"old_string": "x = 10", "new_string": "x = 11"}],
            workdir=workspace_dir,
        )
        assert res_multi.get("status") == "error"
        assert res_multi.get("requires_read_first") is True

        # edit_code_block without reading must fail RBW
        res_block = edit_code_block(
            path="target.py",
            block_to_find="y = 20\n",
            new_block="y = 21\n",
            workdir=str(workspace_dir),
        )
        assert res_block.get("status") == "error"
        assert "not been read" in res_block.get("error", "")

        # Now read the file
        res_read = read_file(path="target.py", workdir=workspace_dir)
        assert res_read.get("status") == "ok"

        # Now multiedit succeeds
        res_multi2 = multiedit(
            path="target.py",
            edits=[{"old_string": "x = 10", "new_string": "x = 11"}],
            workdir=workspace_dir,
        )
        assert res_multi2.get("status") == "ok"

        # edit_code_block succeeds
        res_block2 = edit_code_block(
            path="target.py",
            block_to_find="y = 20\n",
            new_block="y = 21\n",
            workdir=str(workspace_dir),
        )
        assert res_block2.get("status") == "ok"
        assert test_file.read_text(encoding="utf-8") == "x = 11\ny = 21\n"
    finally:
        os.chdir(orig_cwd)


def test_ast_rename_rbw_and_workspace_guard(tmp_path: Path):
    """ast_rename must enforce RBW and not self-mark read."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    py_file = workspace / "mod.py"
    py_file.write_text("def old_func():\n    pass\n", encoding="utf-8")

    # Call ast_rename before reading: must fail
    res = ast_rename(
        path="mod.py",
        old_name="old_func",
        new_name="new_func",
        workdir=str(workspace),
    )
    assert res.get("status") == "error"
    assert "not been read" in res.get("error", "")

    # Read file
    read_file(path="mod.py", workdir=workspace)

    # Now rename succeeds
    res_ok = ast_rename(
        path="mod.py",
        old_name="old_func",
        new_name="new_func",
        workdir=str(workspace),
    )
    assert res_ok.get("status") == "ok"
    assert "def new_func():" in py_file.read_text(encoding="utf-8")


def test_directory_snapshot_and_rollback(tmp_path: Path):
    """Rollback of a deleted directory must restore all nested files and subdirectories."""
    workspace = tmp_path / "ws_rollback"
    workspace.mkdir()

    sub_dir = workspace / "nested" / "deep"
    sub_dir.mkdir(parents=True)
    file1 = sub_dir / "file1.txt"
    file1.write_text("hello file 1", encoding="utf-8")
    file2 = sub_dir / "file2.txt"
    file2.write_text("hello file 2", encoding="utf-8")

    mgr = RollbackManager(str(workspace))
    snap_id = mgr.snapshot_files(["nested"])

    # Delete the directory
    import shutil
    shutil.rmtree(workspace / "nested")
    assert not (workspace / "nested").exists()

    # Roll back
    res = mgr.rollback(snap_id)
    assert res.get("ok") is True

    # Check that all nested files and contents are restored
    assert (workspace / "nested").is_dir()
    assert (workspace / "nested" / "deep").is_dir()
    assert file1.exists()
    assert file1.read_text(encoding="utf-8") == "hello file 1"
    assert file2.exists()
    assert file2.read_text(encoding="utf-8") == "hello file 2"


def test_revert_last_tool_directory(tmp_path: Path):
    """revert_last_tool must restore deleted directories via Orchestrator execution."""
    workspace = tmp_path / "ws_revert"
    workspace.mkdir()

    sub = workspace / "data"
    sub.mkdir()
    f = sub / "record.txt"
    f.write_text("sample data", encoding="utf-8")

    from src.tools.tools_config import configure

    orch = Orchestrator(working_dir=str(workspace))
    token = _PARENT_ORCHESTRATOR_VAR.set(orch)
    configure(autonomous_mode=True, autonomous_approve={"*"})
    try:
        # Pre-read directory path so delete_file passes RBW
        read_file(path="data", workdir=workspace)

        del_res = orch.execute_tool({"name": "delete_file", "arguments": {"path": "data"}})
        assert del_res.get("ok") is True
        assert not sub.exists()

        # Revert last tool
        rev_res = revert_last_tool()
        assert rev_res.get("ok") is True
        assert sub.exists()
        assert f.exists()
        assert f.read_text(encoding="utf-8") == "sample data"
    finally:
        configure(autonomous_mode=False, autonomous_approve=set())
        _PARENT_ORCHESTRATOR_VAR.reset(token)


def test_rename_file_tracks_both_paths(tmp_path: Path):
    """rename_file must track both src and dst in session modified, read, and affected files."""
    workspace = tmp_path / "ws_rename"
    workspace.mkdir()

    src_file = workspace / "old.txt"
    src_file.write_text("content", encoding="utf-8")

    orch = Orchestrator(working_dir=str(workspace))
    orch._affected_files = ["old.txt"]

    # Read src_file
    read_file(path="old.txt", workdir=workspace)

    res = orch.execute_tool(
        {"name": "rename_file", "arguments": {"path": "old.txt", "new_path": "new.txt"}}
    )
    assert res.get("ok") is True

    src_res = str(src_file.resolve())
    dst_res = str((workspace / "new.txt").resolve())

    assert src_res in orch._session_modified_files
    assert dst_res in orch._session_modified_files
    assert src_res in orch._session_read_files
    assert dst_res in orch._session_read_files
    assert dst_res in orch._affected_files or "new.txt" in orch._affected_files
