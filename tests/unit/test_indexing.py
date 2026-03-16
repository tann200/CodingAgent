import pytest
from src.core.indexing.repo_indexer import index_repository
from src.core.indexing.vector_store import VectorStore

@pytest.fixture
def test_repo(tmp_path):
    # Create a dummy repo structure
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main():\n    print('hello')\n\nclass App:\n    pass")
    (tmp_path / "src" / "utils.py").write_text("def helper():\n    return 1")
    return tmp_path

def test_repo_indexer(test_repo):
    index = index_repository(str(test_repo))
    
    assert len(index["files"]) == 2
    assert len(index["symbols"]) == 3 # main, App, helper
    
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
    assert results[0]["symbol_name"] in ["main", "App"]
    
    # 3. Test incremental indexing
    # Add a new file
    (test_repo / "src" / "new.py").write_text("def new_func():\n    pass")
    new_index = index_repository(str(test_repo))
    
    # This should only index the new function
    vs.index_code(new_index)
    
    results = vs.search("a new function")
    assert len(results) > 0
    assert results[0]["symbol_name"] == "new_func"
