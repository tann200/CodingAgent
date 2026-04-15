import asyncio
from types import SimpleNamespace


class DummyToolRegistry(dict):
    def get(self, name):
        return super().get(name)


def test_retrieve_context_merges_search_code_and_symbols(monkeypatch, tmp_path):
    from src.core.orchestration.graph.nodes.perception_node import _retrieve_context

    # Create a fake orchestrator with a simple tool_registry
    called = {"search": [], "symbol": []}

    def search_code_fn(query=None, workdir=None):
        called["search"].append(query)
        return {"results": [{"file_path": "a.py", "snippet": "def a(): pass"}]}

    def find_symbol_fn(name=None, workdir=None):
        called["symbol"].append(name)
        return {"file_path": "b.py", "snippet": "class B: pass"}

    tool_registry = DummyToolRegistry()
    tool_registry["search_code"] = {"fn": search_code_fn}
    tool_registry["find_symbol"] = {"fn": find_symbol_fn}

    orchestrator = SimpleNamespace(tool_registry=tool_registry)

    state = {
        "rounds": 0,
        "task": "Find symbol FooClass in project",
        "working_dir": str(tmp_path),
    }

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(_retrieve_context(state, orchestrator))
    finally:
        loop.close()

    # Expect merged snippets from both search_code and find_symbol
    paths = {r.get("file_path") for r in results}
    assert "a.py" in paths
    assert "b.py" in paths
    assert called["search"]
    assert called["symbol"]
