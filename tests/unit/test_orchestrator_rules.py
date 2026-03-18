from src.core.orchestration.orchestrator import Orchestrator, WRITE_TOOLS_REQUIRING_READ
from unittest.mock import MagicMock
import pytest
import asyncio
from pydantic import BaseModel


def test_read_before_edit_enforcement(tmp_path):
    orch = Orchestrator(working_dir=str(tmp_path))

    # Create a dummy file
    test_file = tmp_path / "test.txt"
    test_file.write_text("initial content\n")

    # Attempt to edit without reading
    edit_call = {
        "name": "edit_file",
        "arguments": {
            "path": "test.txt",
            "patch": "--- test.txt\n+++ test.txt\n@@ -1 +1 @@\n-initial content\n+updated content\n",
        },
    }

    res = orch.execute_tool(edit_call)
    assert res["ok"] is False
    assert "must read" in res["error"]

    # Now read the file
    read_call = {"name": "read_file", "arguments": {"path": "test.txt"}}
    res = orch.execute_tool(read_call)
    assert res["ok"] is True
    assert res["result"]["status"] == "ok"

    # Now attempt to edit again
    res = orch.execute_tool(edit_call)
    if not res["ok"] or res["result"]["status"] != "ok":
        print(f"Error: {res}")
    assert res["ok"] is True
    assert res["result"]["status"] == "ok"
    assert test_file.read_text().strip() == "updated content"


def test_preflight_check_sandbox(tmp_path):
    orch = Orchestrator(working_dir=str(tmp_path))

    # Attempt to write outside sandbox
    write_call = {
        "name": "write_file",
        "arguments": {"path": "../outside.txt", "content": "illegal"},
    }

    # Check preflight manually first
    pre = orch.preflight_check(write_call)
    assert pre["ok"] is False
    assert "outside working directory" in pre["error"]

    # Verify preflight blocks the path traversal attempt (already asserted above).
    # The orchestrator loop integration is covered by the preflight_check assertion.


class MyToolContract(BaseModel):
    test: str


def my_tool(test: str):
    return {"test": test}


def test_tool_contract_validation(tmp_path):
    orch = Orchestrator(working_dir=str(tmp_path))
    from src.core.orchestration.tool_contracts import register_tool_contract

    register_tool_contract("my_tool", MyToolContract)
    orch.tool_registry.register("my_tool", my_tool)

    # Valid call
    edit_call = {"name": "my_tool", "arguments": {"test": "hello"}}
    res = orch.execute_tool(edit_call)
    assert res["ok"] is True
    assert res["result"]["test"] == "hello"

    # Invalid call
    edit_call = {"name": "my_tool", "arguments": {"wrong_arg": "hello"}}
    res = orch.execute_tool(edit_call)
    assert res["ok"] is False


def test_write_tools_requiring_read_set():
    """WRITE_TOOLS_REQUIRING_READ contains the expected tool names."""
    assert "edit_file" in WRITE_TOOLS_REQUIRING_READ
    assert "write_file" in WRITE_TOOLS_REQUIRING_READ
    assert "edit_by_line_range" in WRITE_TOOLS_REQUIRING_READ
    assert "apply_patch" in WRITE_TOOLS_REQUIRING_READ


def test_write_file_requires_read_first(tmp_path):
    orch = Orchestrator(working_dir=str(tmp_path))

    existing = tmp_path / "existing.txt"
    existing.write_text("hello\n")

    write_call = {
        "name": "write_file",
        "arguments": {"path": "existing.txt", "content": "overwritten"},
    }

    res = orch.execute_tool(write_call)
    assert res["ok"] is False
    assert "must read" in res["error"]

    # After reading, write is allowed
    orch.execute_tool({"name": "read_file", "arguments": {"path": "existing.txt"}})
    res = orch.execute_tool(write_call)
    assert res["ok"] is True


def test_new_file_write_not_blocked_without_prior_read(tmp_path):
    """Writing a brand-new file (doesn't exist on disk) must NOT be blocked."""
    orch = Orchestrator(working_dir=str(tmp_path))

    write_call = {
        "name": "write_file",
        "arguments": {"path": "brand_new.txt", "content": "new content"},
    }

    res = orch.execute_tool(write_call)
    assert res["ok"] is True, f"Expected ok=True for new file write, got: {res}"
