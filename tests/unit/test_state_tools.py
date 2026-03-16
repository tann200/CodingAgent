import pytest
import json
import os
import tempfile
from pathlib import Path
from src.tools.state_tools import (
    create_state_checkpoint,
    list_checkpoints,
    restore_state_checkpoint,
    diff_state,
    batched_file_read,
    multi_file_summary,
)


@pytest.fixture
def tmp_workdir(tmp_path):
    agent_context = tmp_path / ".agent-context"
    agent_context.mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_create_state_checkpoint(tmp_workdir):
    result = create_state_checkpoint(
        current_task="Fix the login bug",
        tool_call_history=[{"tool": "read_file", "args": {"path": "auth.py"}}],
        modified_files=["auth.py"],
        reasoning_summary="Found the issue in auth.py line 42",
        workdir=str(tmp_workdir),
    )
    assert result["status"] == "ok"
    assert "checkpoint_id" in result
    assert result["checkpoint_id"].startswith("checkpoint_")


def test_list_checkpoints_empty(tmp_workdir):
    result = list_checkpoints(workdir=str(tmp_workdir))
    assert result["status"] == "ok"
    assert result["checkpoints"] == []


def test_list_checkpoints_with_data(tmp_workdir):
    create_state_checkpoint(
        current_task="Task 1",
        tool_call_history=[],
        modified_files=[],
        reasoning_summary="summary",
        workdir=str(tmp_workdir),
    )
    result = list_checkpoints(workdir=str(tmp_workdir))
    assert result["status"] == "ok"
    assert len(result["checkpoints"]) == 1
    assert result["checkpoints"][0]["current_task"] == "Task 1"


def test_restore_state_checkpoint(tmp_workdir):
    create_result = create_state_checkpoint(
        current_task="Test task",
        tool_call_history=[{"tool": "read_file", "args": {"path": "a.py"}}],
        modified_files=["a.py"],
        reasoning_summary="Working on it",
        workdir=str(tmp_workdir),
    )
    checkpoint_id = create_result["checkpoint_id"]

    restore_result = restore_state_checkpoint(checkpoint_id, workdir=str(tmp_workdir))
    assert restore_result["status"] == "ok"
    assert restore_result["current_task"] == "Test task"
    assert len(restore_result["tool_call_history"]) == 1


def test_restore_nonexistent_checkpoint(tmp_workdir):
    result = restore_state_checkpoint(
        "checkpoint_nonexistent", workdir=str(tmp_workdir)
    )
    assert result["status"] == "error"
    assert "not found" in result["error"]


def test_diff_state(tmp_workdir):
    cp1 = create_state_checkpoint(
        current_task="Initial task",
        tool_call_history=[{"tool": "read_file"}],
        modified_files=["a.py"],
        reasoning_summary="step 1",
        workdir=str(tmp_workdir),
    )
    cp2 = create_state_checkpoint(
        current_task="Updated task",
        tool_call_history=[{"tool": "read_file"}, {"tool": "edit_file"}],
        modified_files=["a.py", "b.py"],
        reasoning_summary="step 2",
        workdir=str(tmp_workdir),
    )

    diff_result = diff_state(
        cp1["checkpoint_id"], cp2["checkpoint_id"], workdir=str(tmp_workdir)
    )
    assert diff_result["status"] == "ok"
    assert diff_result["diff"]["tasks_different"] == True
    assert diff_result["diff"]["tool_calls_added"] == 1
    assert "b.py" in diff_result["diff"]["files_modified_added"]


def test_batched_file_read_multiple_files(tmp_workdir):
    (tmp_workdir / "file1.py").write_text("print('hello')")
    (tmp_workdir / "file2.py").write_text("print('world')")

    result = batched_file_read(
        paths=["file1.py", "file2.py"],
        workdir=str(tmp_workdir),
    )
    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["files"]["file1.py"]["content"] == "print('hello')"
    assert result["files"]["file2.py"]["content"] == "print('world')"


def test_batched_file_read_nonexistent(tmp_workdir):
    result = batched_file_read(
        paths=["nonexistent.py"],
        workdir=str(tmp_workdir),
    )
    assert result["status"] == "ok"
    assert "nonexistent.py" in result["files"]
    assert result["files"]["nonexistent.py"]["error"] == "File not found"


def test_batched_file_read_size_limit(tmp_workdir):
    large_content = "x" * 20000
    (tmp_workdir / "large.py").write_text(large_content)

    result = batched_file_read(
        paths=["large.py"],
        workdir=str(tmp_workdir),
        max_file_size=10000,
    )
    assert result["status"] == "ok"
    assert "File too large" in result["files"]["large.py"]["error"]


def test_multi_file_summary(tmp_workdir):
    (tmp_workdir / "small.py").write_text("line1\nline2\nline3")
    (tmp_workdir / "empty.py").write_text("")

    result = multi_file_summary(
        paths=["small.py", "empty.py"],
        workdir=str(tmp_workdir),
    )
    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["summaries"]["small.py"]["size_bytes"] > 0
    assert result["summaries"]["small.py"]["size_lines"] == 3


def test_multi_file_summary_nonexistent(tmp_workdir):
    result = multi_file_summary(
        paths=["nonexistent.py"],
        workdir=str(tmp_workdir),
    )
    assert result["status"] == "ok"
    assert "nonexistent.py" in result["summaries"]
    assert result["summaries"]["nonexistent.py"]["error"] == "File not found"
