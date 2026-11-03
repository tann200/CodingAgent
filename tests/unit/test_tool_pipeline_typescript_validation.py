"""Unit tests for SPEC-P1.1 (TypeScript sandbox syntax validation) and SPEC-P1.7 (atomic edit AST validation)."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from src.core.orchestration.tool_execution_pipeline import _run_sandbox_and_snapshot


def test_typescript_file_with_interfaces_passes_sandbox_validation(tmp_path):
    """SPEC-P1.1: Valid TypeScript code with interfaces and types must not be blocked by node --check."""
    orch = MagicMock()
    orch.working_dir = str(tmp_path)
    tool = {"name": "write_file", "side_effects": ["write"]}
    ts_code = """
interface UserConfig {
    id: string;
    retries: number;
    enabled?: boolean;
}

type StringOrNumber = string | number;

export function configure(cfg: UserConfig): StringOrNumber {
    return cfg.id;
}
"""
    args = {"path": str(tmp_path / "config.ts"), "content": ts_code}
    result = _run_sandbox_and_snapshot(orch, "write_file", args, tool, str(tmp_path / "config.ts"))
    assert result is None, f"Expected validation to pass, but got: {result}"


def test_javascript_syntax_error_is_blocked(tmp_path):
    """Validates that plain JS with actual syntax error is still caught by node --check if node is installed."""
    import shutil
    if not shutil.which("node"):
        pytest.skip("node binary not available")

    orch = MagicMock()
    orch.working_dir = str(tmp_path)
    tool = {"name": "write_file", "side_effects": ["write"]}
    bad_js = "function bad( { return 1;"
    args = {"path": str(tmp_path / "bad.js"), "content": bad_js}
    result = _run_sandbox_and_snapshot(orch, "write_file", args, tool, str(tmp_path / "bad.js"))
    assert result is not None
    assert result.get("ok") is False
    assert "syntax error" in result.get("error", "").lower()


def test_edit_file_atomic_computes_candidate_and_catches_python_syntax_error(tmp_path):
    """SPEC-P1.7: edit_file_atomic computes replacement candidate and catches syntax errors pre-write."""
    py_file = tmp_path / "module.py"
    py_file.write_text("def valid_fn():\n    return 42\n", encoding="utf-8")

    orch = MagicMock()
    orch.working_dir = str(tmp_path)
    tool = {"name": "edit_file_atomic", "side_effects": ["write"]}

    # Malformed replacement
    bad_args = {
        "path": str(py_file),
        "old_string": "    return 42",
        "new_string": "    return (broken syntax {{{",
    }
    result = _run_sandbox_and_snapshot(orch, "edit_file_atomic", bad_args, tool, str(py_file))
    assert result is not None
    assert result.get("ok") is False
    assert "syntax error" in result.get("error", "").lower()

    # Valid replacement
    good_args = {
        "path": str(py_file),
        "old_string": "    return 42",
        "new_string": "    return 100",
    }
    good_result = _run_sandbox_and_snapshot(orch, "edit_file_atomic", good_args, tool, str(py_file))
    assert good_result is None
