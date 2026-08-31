"""Unit tests for VectorStore episodic-memory persistence (Mem-4)."""

import json
from pathlib import Path

from src.core.indexing import vector_store
from src.core.indexing.vector_store import VectorStore


def _mem_path(tmp_path) -> Path:
    return Path(tmp_path) / ".codingAgent" / "vectorstore" / "memories.jsonl"


def test_add_memory_persists_record_with_expected_schema(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add_memory("Task: fix login. State: in_progress.", {"current_task": "login"})

    path = _mem_path(tmp_path)
    assert path.exists()
    records = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(records) == 1
    rec = records[0]
    assert rec["text"] == "Task: fix login. State: in_progress."
    assert rec["content"] == "Task: fix login. State: in_progress."
    assert rec["metadata"]["current_task"] == "login"
    assert rec["id"]
    assert "created_at" in rec


def test_add_memory_dedup_same_text(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add_memory("Task: hello world", {})
    vs.add_memory("Task: hello world", {})

    path = _mem_path(tmp_path)
    records = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(records) == 1


def test_add_memory_skips_empty_text(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add_memory("", {})
    path = _mem_path(tmp_path)
    assert not path.exists()


def test_search_memories_returns_matching_record_with_text_and_content(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add_memory("Task: fix the authentication flow.", {})
    results = vs.search_memories("authentication", limit=5)
    assert len(results) == 1
    assert results[0]["text"].startswith("Task: fix the authentication")
    assert results[0]["content"].startswith("Task: fix the authentication")
    # vector field stripped
    assert "vector" not in results[0]


def test_search_memories_cross_session_retrieval(tmp_path):
    # Simulate two sessions persisting into the same workspace.
    vs1 = VectorStore(str(tmp_path))
    vs1.add_memory("Task: refactor payment module.", {})
    vs2 = VectorStore(str(tmp_path))
    vs2.add_memory("Task: add audit logging.", {})

    results = vs2.search_memories("payment", limit=5)
    texts = [r.get("text", "") for r in results]
    assert any("payment module" in t for t in texts)


def test_search_memories_empty_when_no_match(tmp_path):
    vs = VectorStore(str(tmp_path))
    vs.add_memory("Task: about zzz unique", {})
    assert vs.search_memories("completely-unrelated-query") == []


def test_search_memories_corrupt_or_missing_file_returns_empty(tmp_path):
    vs = VectorStore(str(tmp_path))
    # Missing file
    assert vs.search_memories("anything") == []
    # Corrupt file
    path = _mem_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad json\n{also bad\n")
    assert vs.search_memories("anything") == []


def test_memory_rotation_caps_record_count(tmp_path):
    vs = VectorStore(str(tmp_path))
    cap = vector_store._MEMORY_MAX_RECORDS
    for i in range(cap + 25):
        vs.add_memory(f"Task item {i} unique wordage", {})
    path = _mem_path(tmp_path)
    records = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(records) == cap
