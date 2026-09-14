"""Unit tests for async / background VectorStore model loading (PHASE-4 4.8).

Covers:
- ``get_st_model_async`` — event-loop-safe model load via ``asyncio.to_thread``.
- Single-flight model loading — concurrent callers share one load.
- ``_get_st_model_ready`` — non-blocking readiness probe.
- ``_preload_st_model`` — idempotent daemon-thread preload that never blocks
  the caller.
- VectorStore ``a*`` wrappers — parity with their synchronous counterparts.
"""

import asyncio
import sys
import threading
import time
import types

from src.core.indexing import vector_store as vs_mod
from src.core.indexing.vector_store import VectorStore, get_st_model_async


def test_get_st_model_ready_is_nonblocking_when_unprobed(monkeypatch):
    monkeypatch.setattr(vs_mod, "_ST_AVAILABLE", None)
    monkeypatch.setattr(vs_mod, "_ST_MODEL", None)
    # Must return immediately (None) without triggering a load.
    assert vs_mod._get_st_model_ready() is None


def test_get_st_model_ready_returns_loaded_model(monkeypatch):
    monkeypatch.setattr(vs_mod, "_ST_AVAILABLE", True)
    monkeypatch.setattr(vs_mod, "_ST_MODEL", "fake-model")
    assert vs_mod._get_st_model_ready() == "fake-model"


def test_get_st_model_async_runs_off_the_event_loop(monkeypatch):
    thread_ids: list[int] = []

    def fake_load() -> str:
        thread_ids.append(threading.get_ident())
        return "model"

    monkeypatch.setattr(vs_mod, "_load_st_model", fake_load)
    main_id = threading.get_ident()

    async def run():
        return await get_st_model_async()

    result = asyncio.run(run())
    assert result == "model"
    assert thread_ids, "loader should have executed"
    assert thread_ids[0] != main_id, "loader must not run on the event loop thread"


def test_real_loader_is_single_flight_under_concurrency(monkeypatch):
    """Concurrent _get_st_model() calls share exactly one model load."""
    load_calls: list[int] = []

    class FakeSentenceTransformer:
        def __init__(self, name: str) -> None:
            load_calls.append(1)
            time.sleep(0.05)
            self.name = name

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    monkeypatch.setattr(vs_mod, "_ST_AVAILABLE", None)
    monkeypatch.setattr(vs_mod, "_ST_MODEL", None)

    results: list[object] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(vs_mod._get_st_model())
        )
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(load_calls) == 1
    assert len(results) == 8
    assert all(getattr(r, "name", None) == "all-MiniLM-L6-v2" for r in results)
    # reset session state so later stub-path tests are unaffected
    monkeypatch.setattr(vs_mod, "_ST_AVAILABLE", None)
    monkeypatch.setattr(vs_mod, "_ST_MODEL", None)


def test_preload_starts_one_daemon_thread_and_does_not_block(monkeypatch):
    started: list[int] = []
    thread_ids: list[int] = []
    ready = threading.Event()

    def fake_load() -> str:
        thread_ids.append(threading.get_ident())
        ready.wait()
        started.append(1)
        return "model"

    monkeypatch.setattr(vs_mod, "_load_st_model", fake_load)
    monkeypatch.setattr(vs_mod, "_ST_AVAILABLE", None)
    monkeypatch.setattr(vs_mod, "_ST_MODEL", None)
    monkeypatch.setattr(vs_mod, "_PRELOAD_STARTED", False)

    # Both calls must return immediately while the load is still blocked.
    vs_mod._preload_st_model()
    vs_mod._preload_st_model()
    assert not started, "preload thread must not block the caller"

    # The spawned thread is alive (blocked in ready.wait): verify it is a
    # daemon before releasing it — otherwise it would keep the interpreter up.
    pid = thread_ids[0]
    thread = next(t for t in threading.enumerate() if t.ident == pid)
    assert thread.daemon

    ready.set()
    deadline = time.time() + 2.0
    while time.time() < deadline and not started:
        time.sleep(0.01)

    assert len(started) == 1, "preload must spawn exactly one thread"


def test_vectorstore_async_wrappers_match_sync(tmp_path):
    workdir = str(tmp_path)
    vs = VectorStore(workdir)

    repo_index = {
        "symbols": [
            {
                "symbol_name": "MyFunc",
                "file_path": "src/foo.py",
                "vector": [0.1, 0.2],
            }
        ]
    }
    asyncio.run(vs.aindex_code(repo_index))
    async_search = asyncio.run(vs.asearch("MyFunc", limit=5))
    assert async_search == vs.search("MyFunc", limit=5)
    assert len(async_search) == 1
    assert "vector" not in async_search[0]

    asyncio.run(vs.aadd_memory("task about login flow", {"k": 1}))
    async_mems = asyncio.run(vs.asearch_memories("login", limit=5))
    assert async_mems == vs.search_memories("login", limit=5)
    assert len(async_mems) == 1
    assert "vector" not in async_mems[0]
