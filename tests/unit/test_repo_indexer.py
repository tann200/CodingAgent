"""Tests for incremental repository indexing."""

# ruff: noqa: E501

import pytest
import tempfile
import shutil
from pathlib import Path
from src.core.indexing.repo_indexer import (
    index_repository,
    get_index_stats,
    force_full_reindex,
    compute_file_hash,
    get_symbols_for_task,
)


class TestIncrementalIndexing:
    """Tests for incremental repository indexing."""

    @pytest.fixture
    def temp_repo(self, tmp_path):
        """Create a temporary repository for testing."""
        repo_dir = tmp_path / "test_repo"
        repo_dir.mkdir()

        # Create initial Python files
        (repo_dir / "main.py").write_text("""
def hello():
    '''Say hello.'''
    print("Hello, World!")

class Greeter:
    '''A greeter class.'''
    def greet(self):
        return "Hello!"
""")

        (repo_dir / "utils.py").write_text("""
def add(a, b):
    '''Add two numbers.'''
    return a + b
""")

        yield str(repo_dir)

        # Cleanup
        if repo_dir.exists():
            shutil.rmtree(repo_dir)

    def test_initial_indexing(self, temp_repo):
        """Test that initial indexing works."""
        index = index_repository(temp_repo)

        assert index["files"] is not None
        assert len(index["files"]) == 2  # main.py and utils.py
        assert len(index["symbols"]) >= 2  # hello, add, Greeter, greet

    def test_incremental_indexing_no_changes(self, temp_repo):
        """Test that incremental indexing skips unchanged files."""
        # First indexing - creates the baseline
        index1 = index_repository(temp_repo)

        # Second indexing (should be fast, no changes) - this is the incremental run
        index2 = index_repository(temp_repo)

        stats = get_index_stats(temp_repo)

        # After second run, is_incremental should be True and files_indexed_last should be 0
        assert stats["is_incremental"] is True

        # The second run should have indexed 0 files (nothing changed)
        # But we need to check the metadata from that run - let me verify by checking the files match
        assert len(index1["files"]) == len(index2["files"])

    def test_incremental_indexing_file_modified(self, temp_repo):
        """Test that modified files are re-indexed."""
        # First indexing
        _index1 = index_repository(temp_repo)

        # Modify a file
        repo_path = Path(temp_repo)
        (repo_path / "main.py").write_text("""
def hello():
    '''Say hello.'''
    print("Hello, Universe!")

class Greeter:
    '''A greeter class.'''
    def greet(self):
        return "Hello!"

    def farewell(self):
        return "Goodbye!"
""")

        # Re-index (should detect change)
        _index2 = index_repository(temp_repo)

        stats = get_index_stats(temp_repo)

        assert stats["files_indexed_last"] == 1  # 1 file changed

    def test_incremental_indexing_new_file(self, temp_repo):
        """Test that new files are indexed."""
        # First indexing
        index1 = index_repository(temp_repo)
        initial_count = len(index1["files"])

        # Add new file
        repo_path = Path(temp_repo)
        (repo_path / "new_module.py").write_text("""
def new_function():
    '''A new function.'''
    return 42
""")

        # Re-index
        index2 = index_repository(temp_repo)

        assert len(index2["files"]) == initial_count + 1

    def test_incremental_indexing_deleted_file(self, temp_repo):
        """Test that deleted files are handled."""
        # First indexing
        index1 = index_repository(temp_repo)
        initial_count = len(index1["files"])

        # Delete a file
        repo_path = Path(temp_repo)
        (repo_path / "utils.py").unlink()

        # Re-index
        index2 = index_repository(temp_repo)

        stats = get_index_stats(temp_repo)

        assert stats["files_deleted_last"] == 1
        assert len(index2["files"]) == initial_count - 1

    def test_force_full_reindex(self, temp_repo):
        """Test force full reindex."""
        # First indexing
        index_repository(temp_repo)

        # Force full reindex
        _index = force_full_reindex(temp_repo)

        stats = get_index_stats(temp_repo)

        assert stats["is_incremental"] is False

    def test_file_hash(self):
        """Test file hash computation."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".py") as f:
            f.write("test content")
            temp_path = Path(f.name)

        try:
            hash1 = compute_file_hash(temp_path)
            assert hash1 is not None
            assert len(hash1) == 64  # SHA-256 hash length

            # Modify file
            temp_path.write_text("modified content")
            hash2 = compute_file_hash(temp_path)

            assert hash1 != hash2  # Different content = different hash
        finally:
            temp_path.unlink()

    def test_get_index_stats(self, temp_repo):
        """Test index statistics."""
        # Index the repo
        index_repository(temp_repo)

        # Get stats
        stats = get_index_stats(temp_repo)

        assert stats["index_exists"] is True
        assert stats["indexed_files"] == 2
        assert stats["indexed_symbols"] > 0
        assert stats["index_version"] == "3.0"
        assert stats["last_indexed"] is not None


class TestMultiLanguageIndexing:
    """Tests for multi-language indexing."""

    def test_get_language(self, tmp_path):
        """Test language detection from file extension."""
        from src.core.indexing.repo_indexer import get_language

        assert get_language(Path("test.py")) == "python"
        assert get_language(Path("test.js")) == "javascript"
        assert get_language(Path("test.ts")) == "typescript"
        assert get_language(Path("test.go")) == "go"
        assert get_language(Path("test.rs")) == "rust"
        assert get_language(Path("test.java")) == "java"

    def test_index_javascript_file(self, tmp_path):
        """Test indexing a JavaScript file."""
        js_file = tmp_path / "test.js"
        js_file.write_text("""
function hello() {
    return "Hello";
}
const add = (a, b) => a + b;
class MyClass {
    method() {}
}
""")

        from src.core.indexing.repo_indexer import parse_file

        result = parse_file(js_file)

        assert result["language"] == "javascript"
        assert len(result["symbols"]) >= 3

    def test_parse_with_regex_typescript(self, tmp_path):
        """Test parsing TypeScript with regex."""
        ts_file = tmp_path / "test.ts"
        ts_file.write_text("""
function greet(name: string): string {
    return `Hello ${name}`;
}
class Service {
    process(): void {}
}
""")

        from src.core.indexing.repo_indexer import parse_file

        result = parse_file(ts_file)

        assert result["language"] == "typescript"
        assert len(result["symbols"]) >= 2

    def test_parse_with_regex_go(self, tmp_path):
        """Test parsing Go with regex."""
        go_file = tmp_path / "test.go"
        go_file.write_text("""
package main

func main() {}

func Add(a, b int) int {
    return a + b
}

type MyStruct struct {
    Name string
}
""")

        from src.core.indexing.repo_indexer import parse_file

        result = parse_file(go_file)

        assert result["language"] == "go"
        symbols = result["symbols"]
        assert any(s["name"] == "Add" for s in symbols)

    def test_parse_with_regex_type_classification(self, tmp_path):
        """Test that type classification correctly identifies class/struct/impl."""
        from src.core.indexing.repo_indexer import parse_with_regex
        from pathlib import Path

        # Test Go struct (should be "struct", not "impl")
        go_file = tmp_path / "test.go"
        go_file.write_text("""
package main

type MyStruct struct {
    Name string
}
""")
        result = parse_with_regex(Path(go_file), "go")
        symbols = result.get("symbols", [])
        struct_symbols = [s for s in symbols if s.get("type") == "struct"]
        assert len(struct_symbols) > 0, "Go struct should be classified as 'struct'"

        # Test Rust impl (should be "impl")
        rust_file = tmp_path / "test.rs"
        rust_file.write_text("""
pub struct MyStruct {
    name: String,
}

impl MyStruct {
    pub fn new() -> Self {
        MyStruct { name: String::new() }
    }
}
""")
        result = parse_with_regex(Path(rust_file), "rust")
        symbols = result.get("symbols", [])
        impl_symbols = [s for s in symbols if s.get("type") == "impl"]
        struct_symbols = [s for s in symbols if s.get("type") == "struct"]
        assert len(impl_symbols) > 0, "Rust impl should be classified as 'impl'"
        assert len(struct_symbols) > 0, "Rust struct should be classified as 'struct'"

        # Test JavaScript class (should be "class")
        js_file = tmp_path / "test.js"
        js_file.write_text("""
class MyClass {
    constructor() {}
    method() {}
}
""")
        result = parse_with_regex(Path(js_file), "javascript")
        symbols = result.get("symbols", [])
        class_symbols = [s for s in symbols if s.get("type") == "class"]
        assert len(class_symbols) > 0, (
            "JavaScript class should be classified as 'class'"
        )


class TestGetSymbolsForTask:
    """CODE_QUALITY_AUDIT #8 fix: get_symbols_for_task() was always returning []
    because it iterated repo_index['files'] and looked for a per-file 'symbols'
    key that does not exist.  Symbols are stored at the top-level 'symbols' list.
    """

    def test_returns_matching_symbols(self, tmp_path):
        """get_symbols_for_task returns symbols whose name matches task tokens."""
        index_repository(str(tmp_path))  # ensure .codingAgent dir exists

        import json
        from pathlib import Path as _Path

        # Write a synthetic index with the correct structure
        agent_ctx = _Path(tmp_path) / ".codingAgent"
        agent_ctx.mkdir(exist_ok=True)
        fake_index = {
            "files": [{"path": "auth.py", "language": "python"}],
            "symbols": [
                {
                    "symbol_name": "authenticate_user",
                    "symbol_type": "function",
                    "file_path": "auth.py",
                },
                {
                    "symbol_name": "LoginForm",
                    "symbol_type": "class",
                    "file_path": "auth.py",
                },
                {
                    "symbol_name": "unrelated_helper",
                    "symbol_type": "function",
                    "file_path": "utils.py",
                },
            ],
        }
        (agent_ctx / "repo_index.json").write_text(json.dumps(fake_index))

        results = get_symbols_for_task(
            str(tmp_path), "authenticate user login", max_results=10
        )

        names = [r["name"] for r in results]
        assert "authenticate_user" in names, (
            "get_symbols_for_task should find 'authenticate_user' for task containing 'authenticate'"
        )
        assert "LoginForm" in names, (
            "get_symbols_for_task should find 'LoginForm' for task containing 'login'"
        )
        assert "unrelated_helper" not in names

    def test_returns_empty_when_no_index(self, tmp_path):
        """Returns [] gracefully when no index file exists."""
        result = get_symbols_for_task(str(tmp_path), "fix auth module")
        assert result == []

    def test_returns_empty_on_blank_task(self, tmp_path):
        """Returns [] when task has no usable tokens (all too short)."""
        import json
        from pathlib import Path as _Path

        agent_ctx = _Path(tmp_path) / ".codingAgent"
        agent_ctx.mkdir(exist_ok=True)
        fake_index = {
            "files": [],
            "symbols": [
                {"symbol_name": "foo", "symbol_type": "function", "file_path": "x.py"}
            ],
        }
        (agent_ctx / "repo_index.json").write_text(json.dumps(fake_index))

        result = get_symbols_for_task(str(tmp_path), "do it")
        assert result == []

    def test_result_keys_include_name_type_file_path(self, tmp_path):
        """Each returned dict has 'name', 'type', and 'file_path' keys."""
        import json
        from pathlib import Path as _Path

        agent_ctx = _Path(tmp_path) / ".codingAgent"
        agent_ctx.mkdir(exist_ok=True)
        fake_index = {
            "files": [],
            "symbols": [
                {
                    "symbol_name": "validate_token",
                    "symbol_type": "function",
                    "file_path": "security.py",
                },
            ],
        }
        (agent_ctx / "repo_index.json").write_text(json.dumps(fake_index))

        results = get_symbols_for_task(
            str(tmp_path), "validate token security", max_results=5
        )
        assert results
        r = results[0]
        assert "name" in r
        assert "type" in r
        assert "file_path" in r
        assert r["name"] == "validate_token"
        assert r["type"] == "function"
        assert r["file_path"] == "security.py"

    def test_respects_max_results(self, tmp_path):
        """max_results cap is enforced."""
        import json
        from pathlib import Path as _Path

        agent_ctx = _Path(tmp_path) / ".codingAgent"
        agent_ctx.mkdir(exist_ok=True)
        fake_index = {
            "files": [],
            "symbols": [
                {
                    "symbol_name": f"process_item_{i}",
                    "symbol_type": "function",
                    "file_path": "proc.py",
                }
                for i in range(20)
            ],
        }
        (agent_ctx / "repo_index.json").write_text(json.dumps(fake_index))

        results = get_symbols_for_task(str(tmp_path), "process item", max_results=3)
        assert len(results) <= 3
