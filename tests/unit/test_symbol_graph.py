"""
Dedicated tests for SymbolGraph and IncrementalIndexer.
"""

import pytest
from pathlib import Path
from src.core.indexing.symbol_graph import SymbolGraph, IncrementalIndexer


class TestSymbolGraphParsePython:
    """Tests for Python file symbol extraction."""

    def test_parse_classes(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("class Foo:\n    def bar(self): pass\n    def baz(self): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file(f)
        names = [c["name"] for c in symbols["classes"]]
        assert "Foo" in names

    def test_parse_class_methods(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("class MyClass:\n    def method_a(self): pass\n    def method_b(self): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file(f)
        cls = symbols["classes"][0]
        assert "method_a" in cls["methods"]
        assert "method_b" in cls["methods"]

    def test_parse_functions(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def greet(name): return name\ndef farewell(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file(f)
        names = [fn["name"] for fn in symbols["functions"]]
        assert "greet" in names
        assert "farewell" in names

    def test_parse_imports(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("import os\nfrom pathlib import Path\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file(f)
        assert "os" in symbols["imports"]
        assert "pathlib.Path" in symbols["imports"]

    def test_parse_docstring(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text('"""Module docstring."""\n\ndef foo(): pass\n')
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file(f)
        assert "Module docstring" in symbols["docstring"]

    def test_parse_invalid_file_returns_empty(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def (:\n    pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file(f)
        assert symbols == {"classes": [], "functions": [], "imports": [], "docstring": ""}


class TestSymbolGraphIncrementalUpdate:
    """Tests for update_file() and hash-based caching."""

    def test_update_file_adds_node(self, tmp_path):
        f = tmp_path / "widget.py"
        f.write_text("class Widget: pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        assert "widget.py" in sg.nodes

    def test_update_file_skips_unchanged(self, tmp_path):
        f = tmp_path / "stable.py"
        f.write_text("def stable(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        snapshot = dict(sg.nodes)
        # Second call — no change — must not alter nodes
        sg.update_file(str(f))
        assert sg.nodes == snapshot

    def test_update_file_re_parses_changed_file(self, tmp_path):
        f = tmp_path / "evolving.py"
        f.write_text("def original(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        f.write_text("def updated(): pass\ndef added(): pass\n")
        sg.update_file(str(f))
        fn_names = [fn["name"] for fn in sg.nodes["evolving.py"]["symbols"]["functions"]]
        assert "updated" in fn_names
        assert "added" in fn_names

    def test_update_file_only_accepts_python(self, tmp_path):
        txt = tmp_path / "notes.txt"
        txt.write_text("not python\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(txt))
        assert len(sg.nodes) == 0

    def test_file_hashes_stored_as_strings(self, tmp_path):
        f = tmp_path / "typed.py"
        f.write_text("x = 1\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        for key in sg.file_hashes:
            assert isinstance(key, str), f"Non-string key: {key!r}"

    def test_remove_file(self, tmp_path):
        f = tmp_path / "remove_me.py"
        f.write_text("def bye(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        assert "remove_me.py" in sg.nodes
        sg.remove_file(str(f))
        assert "remove_me.py" not in sg.nodes


class TestSymbolGraphQueries:
    """Tests for query methods."""

    def test_find_calls_returns_matches(self, tmp_path):
        f = tmp_path / "caller.py"
        f.write_text("def compute(): pass\ndef run(): compute()\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        # find_calls finds files containing a function with that name
        results = sg.find_calls("compute")
        assert len(results) >= 1
        assert any(r["file"].endswith("caller.py") for r in results)

    def test_find_calls_no_match(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def foo(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        results = sg.find_calls("nonexistent_function")
        assert results == []

    def test_find_tests_for_module(self, tmp_path):
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        tf = test_dir / "test_mymodule.py"
        tf.write_text("def test_something(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(tf))
        results = sg.find_tests_for_module("mymodule")
        assert len(results) >= 1

    def test_get_symbol_at_line_class(self, tmp_path):
        f = tmp_path / "shape.py"
        f.write_text("class Circle:\n    pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        sym = sg.get_symbol_at_line("shape.py", 1)
        assert sym is not None
        assert sym["type"] == "class"
        assert sym["name"] == "Circle"

    def test_get_all_symbols(self, tmp_path):
        f = tmp_path / "items.py"
        f.write_text("class Item: pass\ndef make(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        symbols = sg.get_all_symbols()
        assert "Item" in symbols
        assert "make" in symbols

    def test_rebuild_index(self, tmp_path):
        (tmp_path / "a.py").write_text("def alpha(): pass\n")
        (tmp_path / "b.py").write_text("def beta(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.rebuild_index(str(tmp_path))
        assert len(sg.nodes) == 2


class TestSymbolGraphPersistence:
    """Tests for save/load cycle."""

    def test_graph_persists_to_disk(self, tmp_path):
        f = tmp_path / "persist.py"
        f.write_text("def saved(): pass\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        assert sg.graph_path.exists()

    def test_graph_loads_from_disk(self, tmp_path):
        f = tmp_path / "loadable.py"
        f.write_text("class Loadable: pass\n")
        sg1 = SymbolGraph(workdir=str(tmp_path))
        sg1.update_file(str(f))

        # Create fresh instance — should load from disk
        sg2 = SymbolGraph(workdir=str(tmp_path))
        assert "loadable.py" in sg2.nodes


class TestIncrementalIndexer:
    """Tests for IncrementalIndexer."""

    def test_check_and_update_single_file(self, tmp_path):
        f = tmp_path / "idx.py"
        f.write_text("def idx(): pass\n")
        indexer = IncrementalIndexer(workdir=str(tmp_path))
        indexer.check_and_update(str(f))
        assert "idx.py" in indexer.symbol_graph.nodes

    def test_get_symbol_info(self, tmp_path):
        f = tmp_path / "lookup.py"
        f.write_text("def lookup_func(): pass\n")
        indexer = IncrementalIndexer(workdir=str(tmp_path))
        indexer.check_and_update(str(f))
        info = indexer.get_symbol_info("lookup_func")
        assert info is not None
        assert info["type"] == "function"

    def test_find_references(self, tmp_path):
        f = tmp_path / "ref.py"
        f.write_text("def ref_func(): pass\n")
        indexer = IncrementalIndexer(workdir=str(tmp_path))
        indexer.check_and_update(str(f))
        refs = indexer.find_references("ref_func")
        assert len(refs) >= 1
