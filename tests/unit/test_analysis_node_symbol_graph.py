import json
from pathlib import Path
import pytest

from src.core.indexing.repo_indexer import (
    index_repository,
    refresh_file_in_index,
)
from src.core.indexing.symbol_graph import SymbolGraph
from src.core.indexing.vector_store import VectorStore
from src.core.orchestration.graph.nodes.analysis_node import analysis_node


def _make_state(tmp_path: Path, **kwargs):
    base = {
        "task": "optimize bar function",
        "history": [],
        "verified_reads": [],
        "rounds": 0,
        "working_dir": str(tmp_path),
        "system_prompt": "",
        "next_action": None,
        "last_result": None,
        "errors": [],
        "current_plan": None,
        "current_step": 0,
        "deterministic": False,
        "seed": None,
        "analysis_summary": None,
        "relevant_files": None,
        "key_symbols": None,
        "call_graph": None,
        "test_map": None,
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": True,
        "task_decomposed": False,
        "tool_call_count": 0,
        "max_tool_calls": 30,
        "repo_summary_data": None,
        "replan_required": None,
        "action_failed": False,
        "plan_progress": None,
        "evaluation_result": None,
        "cancel_event": None,
        "empty_response_count": 0,
        "analyst_findings": None,
        "plan_resumed": None,
        "last_debug_error_type": None,
        "session_id": None,
        "delegation_results": None,
        "delegations": None,
        "last_tool_name": None,
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_analysis_node_call_graph_and_vectorstore(tmp_path: Path):
    """analysis_node must not crash on caller dicts and must populate VectorStore symbols."""
    code_file = tmp_path / "service.py"
    code_file.write_text(
        "def bar():\n"
        "    return 42\n\n"
        "def foo():\n"
        "    return bar()\n",
        encoding="utf-8",
    )

    from unittest.mock import MagicMock

    mock_orch = MagicMock()
    mock_orch.tool_registry.tools = {}

    def fake_tool_call(tool_name, **kwargs):
        if tool_name == "glob":
            return {"status": "ok", "matches": ["service.py"]}
        return None

    mock_orch.tool_registry.call.side_effect = fake_tool_call
    mock_orch.tool_execution_pipeline.execute.side_effect = fake_tool_call
    config = {"configurable": {"orchestrator": mock_orch}}

    state = _make_state(tmp_path, task="optimize bar function")
    new_state = await analysis_node(state, config)

    # Verify VectorStore search finds symbols
    vs = VectorStore(str(tmp_path))
    res = vs.search("bar", limit=5)
    assert any(r.get("symbol_name") == "bar" or r.get("name") == "bar" for r in res)

    # Verify call_graph is populated and was not wiped by TypeError
    call_graph = new_state.get("call_graph")
    assert call_graph is not None
    assert isinstance(call_graph, dict)
    assert "bar" in call_graph
    assert len(call_graph["bar"]) > 0
    # Also check analysis_summary contains formatted symbol context
    summary = new_state.get("analysis_summary", "")
    assert "bar" in summary


def test_repo_indexer_async_and_docstrings_and_delete(tmp_path: Path):
    """refresh_file_in_index must handle async functions, docstrings, metadata, and deletions."""
    app_file = tmp_path / "app.py"
    app_file.write_text(
        'async def fetch_data(url: str):\n'
        '    """Fetch data asynchronously."""\n'
        '    pass\n',
        encoding="utf-8",
    )

    idx = index_repository(str(tmp_path))
    assert len(idx["symbols"]) == 1
    sym = idx["symbols"][0]
    assert sym["symbol_name"] == "fetch_data"
    assert sym["symbol_type"] == "function"
    assert sym["docstring"] == "Fetch data asynchronously."
    assert sym["start_line"] == 1

    # Modify file to add class with docstring
    app_file.write_text(
        'class Client:\n'
        '    """A client class."""\n'
        '    pass\n\n'
        'async def fetch_data(url: str):\n'
        '    """Fetch data asynchronously v2."""\n'
        '    pass\n',
        encoding="utf-8",
    )
    ok = refresh_file_in_index(str(app_file), str(tmp_path))
    assert ok is True

    ctx_dir = tmp_path / ".codingAgent"
    if not ctx_dir.exists():
        ctx_dir = tmp_path / ".agent-context"
    saved = json.loads((ctx_dir / "repo_index.json").read_text(encoding="utf-8"))
    symbols = saved["symbols"]
    assert len(symbols) == 2
    client_sym = next(s for s in symbols if s["symbol_name"] == "Client")
    assert client_sym["docstring"] == "A client class."
    assert client_sym["start_line"] == 1

    meta = json.loads((ctx_dir / "repo_index_meta.json").read_text(encoding="utf-8"))
    assert "file_hashes" in meta
    assert "app.py" in meta["file_hashes"]

    # Delete file and verify refresh_file_in_index cleans up index and meta
    app_file.unlink()
    ok_del = refresh_file_in_index(str(app_file), str(tmp_path))
    assert ok_del is True

    saved_after_del = json.loads((ctx_dir / "repo_index.json").read_text(encoding="utf-8"))
    assert len(saved_after_del["files"]) == 0
    assert len(saved_after_del["symbols"]) == 0

    meta_after_del = json.loads((ctx_dir / "repo_index_meta.json").read_text(encoding="utf-8"))
    assert "app.py" not in meta_after_del["file_hashes"]


def test_symbol_graph_remove_file(tmp_path: Path):
    """SymbolGraph.remove_file must clean up relative and absolute path entries."""
    sg = SymbolGraph(str(tmp_path))
    test_file = tmp_path / "module.py"
    test_file.write_text("def hello(): pass\n", encoding="utf-8")

    sg.update_file(str(test_file))
    assert str(test_file.resolve()) in sg.file_hashes or "module.py" in sg.file_hashes
    assert "module.py" in sg.nodes

    # Remove with relative path
    sg.remove_file("module.py")
    assert str(test_file.resolve()) not in sg.file_hashes
    assert "module.py" not in sg.file_hashes
    assert "module.py" not in sg.nodes
