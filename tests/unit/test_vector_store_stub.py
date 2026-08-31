from pathlib import Path
import json

from src.core.indexing.vector_store import VectorStore


def test_vectorstore_index_and_search(tmp_path):
    workdir = str(tmp_path)
    vs = VectorStore(workdir)

    # Prepare a minimal repo_index structure with symbols
    repo_index = {
        "symbols": [
            {
                "symbol_name": "MyFunc",
                "file_path": "src/foo.py",
                "content": "def MyFunc(): pass",
                "vector": [0.1, 0.2],
            },
            {
                "symbol_name": "Other",
                "file_path": "src/bar.py",
                "content": "def Other(): pass",
                "vector": [0.3, 0.4],
            },
        ]
    }

    vs.index_code(repo_index)

    # Ensure symbols.json persisted
    p = Path(workdir) / ".codingAgent" / "vectorstore" / "symbols.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert isinstance(data, list)
    assert any(s.get("symbol_name") == "MyFunc" for s in data)

    # Search by token that matches symbol name
    results = vs.search("MyFunc", limit=5)
    assert len(results) == 1
    # Ensure the returned result does not include the raw 'vector' payload
    assert "vector" not in results[0]


def test_vectorstore_model_and_memories(tmp_path):
    vs = VectorStore(str(tmp_path))

    # model property should exist and produce deterministic encodings
    model = vs.model
    vec1 = model.encode("hello")[0]
    vec2 = model.encode("hello")[0]
    assert vec1 == vec2
    assert len(vec1) == model.get_sentence_embedding_dimension()

    # add_memory persists and search_memories retrieves (Mem-4)
    vs.add_memory("some text about login", {"meta": 1})
    mems = vs.search_memories("login")
    assert len(mems) == 1
    assert mems[0].get("text") == "some text about login"
    assert mems[0].get("content") == "some text about login"
    # vector is stripped from search results
    assert "vector" not in mems[0]
