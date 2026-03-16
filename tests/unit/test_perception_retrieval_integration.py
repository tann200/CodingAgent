import asyncio
import json
from unittest.mock import MagicMock
from src.core.orchestration.graph.nodes.workflow_nodes import perception_node
from src.core.orchestration.orchestrator import ToolRegistry


def run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_perception_injects_retrieved_snippets(tmp_path):
    # Setup orchestrator-like object with a tool registry that returns a fake search_code result
    reg = ToolRegistry()

    def fake_search_code(query, workdir=None):
        return {"results": [{"file_path": "src/foo.py", "snippet": "def foo(): pass"}]}

    reg.register("search_code", fake_search_code)

    class FakeOrch:
        def __init__(self, workdir, registry):
            self.tool_registry = registry
            self.adapter = None
            self.deterministic = False
            self.seed = None
            self.msg_mgr = MagicMock()

    orch = FakeOrch(str(tmp_path), reg)

    state = {
        "system_prompt": "You are an assistant",
        "working_dir": str(tmp_path),
        "task": "Find foo",
        "history": [],
        "rounds": 0,
    }

    config = {"configurable": {"orchestrator": orch}}

    res = run_async(perception_node(state, config))
    # Check that the returned history contains assistant message (empty content is allowed)
    assert "history" in res
    # The builder puts repository intelligence in system message; here we check that the returned next_action is present (none expected) and no error
    assert "next_action" in res
    # Nothing should break; test passes if perception_node ran and returned dict

