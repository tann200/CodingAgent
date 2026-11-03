import pytest
from unittest.mock import MagicMock
from src.core.orchestration.tool_execution_pipeline import _run_sandbox_and_snapshot


def test_edit_file_atomic_ast_validation_blocks_invalid_python(tmp_path):
    py_file = tmp_path / "valid.py"
    py_file.write_text("def hello():\n    return 42\n", encoding="utf-8")

    orch = MagicMock()
    orch.rollback_manager = None

    tool = {"permission": None, "side_effects": ["write"]}
    args = {
        "path": str(py_file),
        "old_string": "return 42",
        "new_string": "return (\nthis is a broken syntax",
    }

    err = _run_sandbox_and_snapshot(
        orch, "edit_file_atomic", args, tool, path_arg=str(py_file)
    )
    assert err is not None
    assert "syntax error" in err.get("error", "").lower()


def test_edit_file_atomic_ast_validation_allows_valid_python(tmp_path):
    py_file = tmp_path / "valid.py"
    py_file.write_text("def hello():\n    return 42\n", encoding="utf-8")

    orch = MagicMock()
    orch.rollback_manager = None

    tool = {"permission": None, "side_effects": ["write"]}
    args = {
        "path": str(py_file),
        "old_string": "return 42",
        "new_string": "return 100",
    }

    err = _run_sandbox_and_snapshot(
        orch, "edit_file_atomic", args, tool, path_arg=str(py_file)
    )
    assert err is None
