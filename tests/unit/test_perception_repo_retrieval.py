import pytest
from src.core.orchestration.graph.nodes import workflow_nodes
from src.core.context.context_builder import ContextBuilder


class FakeToolRegistry:
    def __init__(self):
        # tools dict used by build_prompt
        self.tools = {
            "search_code": {"description": "search"},
            "find_symbol": {"description": "find"},
            "find_references": {"description": "refs"},
        }

    def get(self, name):
        if name == "search_code":
            return {"fn": lambda **kwargs: [{"file_path": "src/a.py", "snippet": "def foo(): pass"}]}
        if name == "find_symbol":
            return {"fn": lambda **kwargs: {"file_path": "src/b.py", "snippet": "def bar(): pass", "symbol_name": "bar"}}
        if name == "find_references":
            return {"fn": lambda **kwargs: [{"file_path": "src/c.py", "excerpt": "foo() called here"}]}
        return None


class FakeOrch:
    def __init__(self):
        self.tool_registry = FakeToolRegistry()
        self.adapter = None


@pytest.mark.asyncio
async def test_perception_injects_retrieved_snippets(monkeypatch):
    captured = {}

    def fake_build_prompt(self, **kwargs):
        # Capture retrieved_snippets for assertion
        captured['retrieved_snippets'] = kwargs.get('retrieved_snippets')
        # Return a minimal messages list acceptable to call_model
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]

    monkeypatch.setattr(ContextBuilder, 'build_prompt', fake_build_prompt)

    # Mock call_model to avoid real LLM calls
    fake_resp = {"choices": [{"message": {"content": ""}}]}
    monkeypatch.setattr(
        "src.core.orchestration.graph.nodes.perception_node.call_model",
        lambda *args, **kwargs: fake_resp,
    )

    state = {
        'task': 'find foo',
        'history': [],
        'rounds': 0,
        'working_dir': '.',
        'system_prompt': 'You are a helpful assistant',
    }

    conf = {'configurable': {'orchestrator': FakeOrch()}}

    _ = await workflow_nodes.perception_node(state, conf)

    assert 'retrieved_snippets' in captured
    snippets = captured['retrieved_snippets']
    assert isinstance(snippets, list)
    # Expect at least 3 snippets from search_code, find_symbol, find_references
    assert any(s.get('reason') == 'search_code' for s in snippets)
    assert any(s.get('reason') == 'find_symbol' for s in snippets)
    assert any(s.get('reason') == 'find_references' for s in snippets)

