from pathlib import Path
from unittest.mock import MagicMock
from src.core.orchestration.tool_execution_pipeline import _run_sandbox_and_snapshot
from src.tools._lint_verify import is_content_prevalidated


def test_edit_file_atomic_blocks_syntax_error(tmp_path: Path):
    py_file = tmp_path / "valid.py"
    py_file.write_text("def hello():\n    return 42\n", encoding="utf-8")

    orch = MagicMock()
    orch.working_dir = str(tmp_path)
    tool = {"side_effects": ["write"]}

    # Attempt an atomic replacement that introduces a syntax error
    args = {
        "path": str(py_file),
        "old_string": "return 42",
        "new_string": "return (42 +",  # syntax error
    }

    res = _run_sandbox_and_snapshot(
        tool=tool,
        name="edit_file_atomic",
        args=args,
        path_arg=str(py_file),
        orch=orch,
    )

    assert res is not None
    assert res.get("ok") is False
    assert "syntax error" in res.get("error", "").lower()


def test_edit_file_atomic_allows_valid_and_marks_prevalidated(tmp_path: Path):
    py_file = tmp_path / "valid2.py"
    py_file.write_text("def hello():\n    return 42\n", encoding="utf-8")

    orch = MagicMock()
    orch.working_dir = str(tmp_path)
    orch._step_snapshot_id = None
    orch.rollback_manager = MagicMock()
    tool = {"side_effects": ["write"]}

    # Valid replacement
    args = {
        "path": str(py_file),
        "old_string": "return 42",
        "new_string": "return 100",
    }

    res = _run_sandbox_and_snapshot(
        tool=tool,
        name="edit_file_atomic",
        args=args,
        path_arg=str(py_file),
        orch=orch,
    )

    assert res is None  # success
    # Check that candidate content was marked as prevalidated
    expected_content = "def hello():\n    return 100\n"
    assert is_content_prevalidated(py_file, expected_content)


def test_multiedit_blocks_syntax_error(tmp_path: Path):
    py_file = tmp_path / "multi.py"
    py_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    orch = MagicMock()
    orch.working_dir = str(tmp_path)
    tool = {"side_effects": ["write"]}

    args = {
        "path": str(py_file),
        "edits": [
            {"old_string": "def add(a, b):", "new_string": "def add(a, b:"},  # missing closing paren
        ],
    }

    res = _run_sandbox_and_snapshot(
        tool=tool,
        name="multiedit",
        args=args,
        path_arg=str(py_file),
        orch=orch,
    )

    assert res is not None
    assert res.get("ok") is False
    assert "syntax error" in res.get("error", "").lower()
