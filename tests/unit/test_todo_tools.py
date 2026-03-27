"""
Tests for todo_tools.py — manage_todo type coercion and basic operations.
"""

from src.tools.todo_tools import manage_todo


def _create_todo(tmp_path):
    """Helper: create a 3-step TODO list and return the workdir."""
    result = manage_todo(
        action="create",
        workdir=str(tmp_path),
        steps=["Read file", "Edit function", "Run tests"],
    )
    assert result["status"] == "ok"
    return str(tmp_path)


class TestManageTodoTypeCoercion:
    """Regression tests for step_id type coercion (string → int)."""

    def test_check_with_int_step_id(self, tmp_path):
        workdir = _create_todo(tmp_path)
        result = manage_todo(action="check", workdir=workdir, step_id=0)
        assert result["status"] == "ok"
        assert result["done_count"] == 1

    def test_check_with_string_step_id(self, tmp_path):
        """YAML tool calls may pass step_id as a string; must coerce to int."""
        workdir = _create_todo(tmp_path)
        result = manage_todo(action="check", workdir=workdir, step_id="0")
        assert result["status"] == "ok", f"String step_id '0' should be accepted, got: {result}"
        assert result["done_count"] == 1

    def test_check_with_string_step_id_1(self, tmp_path):
        workdir = _create_todo(tmp_path)
        result = manage_todo(action="check", workdir=workdir, step_id="1")
        assert result["status"] == "ok"

    def test_check_invalid_string_step_id(self, tmp_path):
        workdir = _create_todo(tmp_path)
        result = manage_todo(action="check", workdir=workdir, step_id="abc")
        assert result["status"] == "error"
        assert "integer" in result["error"]

    def test_update_with_string_step_id(self, tmp_path):
        workdir = _create_todo(tmp_path)
        result = manage_todo(
            action="update", workdir=workdir, step_id="2", description="Verify changes"
        )
        assert result["status"] == "ok"
        assert result["steps"][2]["description"] == "Verify changes"

    def test_check_out_of_range_string(self, tmp_path):
        workdir = _create_todo(tmp_path)
        result = manage_todo(action="check", workdir=workdir, step_id="99")
        assert result["status"] == "error"
        assert "out of range" in result["error"]


class TestManageTodoBasicOps:
    def test_create_and_read(self, tmp_path):
        manage_todo(action="create", workdir=str(tmp_path), steps=["Step A", "Step B"])
        result = manage_todo(action="read", workdir=str(tmp_path))
        assert result["status"] == "ok"
        assert result["total"] == 2
        assert result["done_count"] == 0

    def test_check_then_read(self, tmp_path):
        manage_todo(action="create", workdir=str(tmp_path), steps=["Step A", "Step B"])
        manage_todo(action="check", workdir=str(tmp_path), step_id=0)
        result = manage_todo(action="read", workdir=str(tmp_path))
        assert result["done_count"] == 1
        assert result["steps"][0]["done"] is True
        assert result["steps"][1]["done"] is False

    def test_clear_removes_files(self, tmp_path):
        manage_todo(action="create", workdir=str(tmp_path), steps=["Step A"])
        manage_todo(action="clear", workdir=str(tmp_path))
        result = manage_todo(action="read", workdir=str(tmp_path))
        assert result["steps"] == []

    def test_create_requires_steps(self, tmp_path):
        result = manage_todo(action="create", workdir=str(tmp_path))
        assert result["status"] == "error"

    def test_check_requires_step_id(self, tmp_path):
        manage_todo(action="create", workdir=str(tmp_path), steps=["Step A"])
        result = manage_todo(action="check", workdir=str(tmp_path))
        assert result["status"] == "error"
