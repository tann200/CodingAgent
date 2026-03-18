from src.core.orchestration.orchestrator import Orchestrator
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

    # Verify orchestrator loop would catch it
    import src.core.llm_manager

    original_call_model = src.core.llm_manager.call_model

    # Use a simpler mocking for call_model
    from unittest.mock import AsyncMock

    mock_call_model = AsyncMock()
    mock_call_model.side_effect = [
        {
            "choices": [
                {
                    "message": {
                        "content": "```yaml\nname: write_file\narguments:\n  path: ../outside.txt\n  content: illegal\n```"
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "Oops, I failed."}}]},
    ]
    src.core.inference.llm_manager.call_model = mock_call_model

    try:
        orch.run_agent_once(None, [{"role": "user", "content": "do it"}], {})

        # Check that the error was reported to the agent in the message history
        msgs = orch.msg_mgr.all()
        error_msg = next(
            (
                m["content"]
                for m in msgs
                if "outside working directory" in m.get("content", "")
            ),
            None,
        )
        assert error_msg is not None
    finally:
        src.core.llm_manager.call_model = original_call_model


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
