import pytest
from src.core.orchestration.orchestrator import Orchestrator
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_langgraph_refactor_read_before_edit(tmp_path):
    # Setup Orchestrator
    orch = Orchestrator(working_dir=str(tmp_path))

    # Create dummy file with newline to satisfy patch command
    test_file = tmp_path / "graph_test.txt"
    test_file.write_text("initial\n")

    # Mock the LLM to try an edit immediately
    from src.core import llm_manager

    original_call = llm_manager.call_model

    mock_responses = [
        # 1. First turn: Agent wants to edit without reading
        {
            "choices": [
                {
                    "message": {
                        "content": '<think>I will edit graph_test.txt</think>\n<tool>\nname: edit_file\nargs: {"path": "graph_test.txt", "patch": "--- graph_test.txt\\n+++ graph_test.txt\\n@@ -1 +1 @@\\n-initial\\n+graph_edited\\n"}\n</tool>'
                    }
                }
            ]
        },
        # 2. Second turn: After receiving error, agent reads the file
        {
            "choices": [
                {
                    "message": {
                        "content": '<think>I forgot to read. Reading now.</think>\n<tool>\nname: read_file\nargs: {"path": "graph_test.txt"}\n</tool>'
                    }
                }
            ]
        },
        # 3. Third turn: Now agent edits again
        {
            "choices": [
                {
                    "message": {
                        "content": '<think>Now I have context. Editing.</think>\n<tool>\nname: edit_file\nargs: {"path": "graph_test.txt", "patch": "--- graph_test.txt\\n+++ graph_test.txt\\n@@ -1 +1 @@\\n-initial\\n+graph_edited\\n"}\n</tool>'
                    }
                }
            ]
        },
        # 4. Fourth turn: Finish
        {"choices": [{"message": {"content": "Task complete."}}]},
    ]

    llm_manager.call_model = AsyncMock(side_effect=mock_responses)

    try:
        # Execute
        res = orch.run_agent_once(None, [{"role": "user", "content": "edit it"}], {})

        # Verify result was successful
        assert "error" not in res

        # Verify the file was eventually edited
        content = test_file.read_text()
        print(f"DEBUG: File content: [{content}]")
        assert "graph_edited" in content

        # Verify the history contains the programmatic error message
        msgs = orch.msg_mgr.all()
        assert any("Logic violation" in str(m["content"]) for m in msgs)

    finally:
        llm_manager.call_model = original_call
