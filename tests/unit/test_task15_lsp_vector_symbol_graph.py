from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def test_lsp_client_cross_loop_lock_restart_and_did_change(tmp_path):
    from src.core.indexing.lsp_client import LSPClient
    from src.core.indexing.lsp_manager import _get_manager_lock, LSPManager

    f = tmp_path / "mod.py"
    f.write_text("x = 1\n", encoding="utf-8")

    client = LSPClient(["python"], tmp_path)
    sent: list[dict] = []

    async def _fake_send(msg):
        async with client._get_lock():
            sent.append(msg)

    client._send = _fake_send  # type: ignore[method-assign]
    client._started = True
    client._proc = SimpleNamespace(returncode=None, stdin=MagicMock())

    # First event loop
    asyncio.run(client.notify_file_changed(f.as_uri(), "x = 1\n"))
    # Second separate event loop must recreate locks seamlessly instead of failing bound-to-different-loop
    asyncio.run(client.notify_file_changed(f.as_uri(), "x = 2\n"))

    assert [m["method"] for m in sent] == ["textDocument/didOpen", "textDocument/didChange"]
    assert sent[1]["params"]["textDocument"]["version"] == 2
    assert sent[1]["params"]["contentChanges"] == [{"text": "x = 2\n"}]

    # Cross-loop LSPManager lock
    mgr = LSPManager(tmp_path)

    async def _use_locks():
        async with _get_manager_lock():
            async with mgr._get_lock():
                return True

    assert asyncio.run(_use_locks()) is True
    assert asyncio.run(_use_locks()) is True

    # Simulate restart clears _open_documents
    client._restart_count = 0
    client._shutting_down = False
    client.start = AsyncMock()  # type: ignore[method-assign]
    asyncio.run(client._restart())
    assert client._open_documents == set()


def test_vector_store_tags_dimensions_and_never_mixes_stub_and_st(monkeypatch, tmp_path):
    from src.core.indexing import vector_store as vs_mod
    from src.core.indexing.vector_store import (
        VectorStore,
        clear_embedding_cache,
        get_embedding_cache_size,
    )

    store = VectorStore(workdir=str(tmp_path))
    store.index_code({"symbols": [{"name": "alpha", "file_path": "a.py"}]})
    sym_file = tmp_path / ".codingAgent" / "vectorstore" / "symbols.json"
    saved = json.loads(sym_file.read_text(encoding="utf-8"))
    assert saved[0]["embed_model"] == "stub-sha256-8d"
    assert saved[0]["embed_dim"] == 8
    assert len(saved[0]["vector"]) == 8

    # Fake 384-dim sentence-transformers model loaded later
    class FakeST:
        model_name = "all-MiniLM-L6-v2"

        def encode(self, texts):
            return [[0.5] * 384 for _ in texts]

    monkeypatch.setattr(vs_mod, "_get_st_model_ready", lambda: FakeST())
    # Must re-encode stale 8-dim stub symbol with 384-dim FakeST cleanly instead of dot-producting 8 vs 384
    hits = store.search("alpha", limit=3)
    assert len(hits) == 1
    assert hits[0]["name"] == "alpha"

    clear_embedding_cache()
    assert get_embedding_cache_size() == 0


def test_symbol_graph_batch_rebuild_walk_and_pagerank(monkeypatch, tmp_path):
    from src.core.indexing.symbol_graph import SymbolGraph
    from src.core.indexing import repo_indexer

    (tmp_path / "core.py").write_text("def core_fn():\n    return 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("import core\n\ndef fa():\n    return core.core_fn()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import core\n\ndef fb():\n    return core.core_fn()\n", encoding="utf-8")
    # Ignored directory file must be skipped by walk_language_files
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    (venv_dir / "ignored.py").write_text("def ignored():\n    pass\n", encoding="utf-8")

    sg = SymbolGraph(workdir=str(tmp_path))
    save_calls = 0
    orig_save = sg._save_graph

    def _counting_save():
        nonlocal save_calls
        save_calls += 1
        return orig_save()

    monkeypatch.setattr(sg, "_save_graph", _counting_save)
    sg.rebuild_index()

    # Batch rebuild must save ONCE, not N times, and exclude .venv
    assert save_calls == 1
    assert "core.py" in sg.nodes and "a.py" in sg.nodes and "b.py" in sg.nodes
    assert not any("ignored.py" in k for k in sg.nodes)
    assert all(not k.startswith("/") for k in sg.file_hashes)

    # core.py imported by both a.py and b.py must rank #1 in PageRank
    ranked = sg.rank_important_files(top_n=3)
    assert ranked[0][0] == "core.py"
    ctx_map = sg.get_call_graph_context(top_n=3)
    assert "core.py" in ctx_map and "core_fn" in ctx_map

    # Write lock exists on repo_indexer
    assert hasattr(repo_indexer, "_INDEX_WRITE_LOCK")
