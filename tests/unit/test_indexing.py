import pytest
from src.core.indexing.repo_indexer import index_repository
from src.core.indexing.vector_store import VectorStore
from src.core.indexing.symbol_graph import SymbolGraph


@pytest.fixture
def test_repo(tmp_path):
    # Create a dummy repo structure
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def main():\n    print('hello')\n\nclass App:\n    pass"
    )
    (tmp_path / "src" / "utils.py").write_text("def helper():\n    return 1")
    return tmp_path


def test_repo_indexer(test_repo):
    index = index_repository(str(test_repo))

    assert len(index["files"]) == 2
    assert len(index["symbols"]) == 3  # main, App, helper

    # Check if index file is created
    assert (test_repo / ".agent-context" / "repo_index.json").exists()


def test_vector_store_indexing_and_search(test_repo):
    # 1. Index
    index = index_repository(str(test_repo))
    vs = VectorStore(str(test_repo))
    vs.index_code(index)

    # Check if db is created
    assert (test_repo / ".agent-context" / "lancedb").exists()

    # 2. Search
    results = vs.search("main entry point")
    assert len(results) > 0
    symbol_names = [r["symbol_name"] for r in results]
    assert any(s in ["main", "App", "helper"] for s in symbol_names)

    # 3. Test incremental indexing
    # Add a new file
    (test_repo / "src" / "new.py").write_text("def new_func():\n    pass")
    new_index = index_repository(str(test_repo))

    # This should only index the new function
    vs.index_code(new_index)

    results = vs.search("a new function")
    assert len(results) > 0
    symbol_names = [r["symbol_name"] for r in results]
    assert "new_func" in symbol_names


class TestSymbolGraphHashBug:
    """Regression tests for the Path vs str hash key bug in SymbolGraph."""

    def test_cache_hits_on_second_update(self, tmp_path):
        """update_file() must not re-parse an unchanged file (cache should hit)."""
        py_file = tmp_path / "module.py"
        py_file.write_text("def foo(): pass\n")

        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(py_file))

        # Manually track parse count via node keys
        nodes_after_first = dict(sg.nodes)

        sg.update_file(str(py_file))  # second call — file unchanged

        # Node updated_at should be identical (no re-parse happened)
        assert sg.nodes == nodes_after_first, (
            "SymbolGraph re-parsed an unchanged file; hash key bug may have regressed"
        )

    def test_file_hashes_stored_as_str_keys(self, tmp_path):
        """Keys in file_hashes must be strings, not Path objects."""
        py_file = tmp_path / "module.py"
        py_file.write_text("class Bar: pass\n")

        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(py_file))

        for key in sg.file_hashes:
            assert isinstance(key, str), f"Expected str key, got {type(key)}: {key!r}"
