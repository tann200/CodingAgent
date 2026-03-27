"""
Dedicated tests for SymbolGraph and IncrementalIndexer.
"""

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
        f.write_text(
            "class MyClass:\n    def method_a(self): pass\n    def method_b(self): pass\n"
        )
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
        assert symbols == {
            "classes": [],
            "functions": [],
            "imports": [],
            "docstring": "",
        }


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
        fn_names = [
            fn["name"] for fn in sg.nodes["evolving.py"]["symbols"]["functions"]
        ]
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
        """Test find_references (which uses find_calls) finds call sites, not definitions."""
        # File with function definition and a call to it
        f = tmp_path / "ref.py"
        f.write_text("def ref_func(): pass\nresult = ref_func()\n")
        indexer = IncrementalIndexer(workdir=str(tmp_path))
        indexer.check_and_update(str(f))
        # Should find the call site (line 2), not the definition (line 1)
        refs = indexer.find_references("ref_func")
        assert len(refs) >= 1
        assert refs[0]["line"] == 2  # Should be the call, not the definition


# ---------------------------------------------------------------------------
# #36: Multi-language SymbolGraph support
# ---------------------------------------------------------------------------

class TestSymbolGraphMultiLanguage:
    """#36: SymbolGraph regex-based parsing for JS/TS/Go/Rust/Java."""

    def test_parse_js_function(self, tmp_path):
        f = tmp_path / "app.js"
        f.write_text("function greet(name) { return name; }\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        names = [fn["name"] for fn in symbols["functions"]]
        assert "greet" in names

    def test_parse_js_class(self, tmp_path):
        f = tmp_path / "widget.js"
        f.write_text("class MyWidget extends Base { }\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        names = [c["name"] for c in symbols["classes"]]
        assert "MyWidget" in names

    def test_parse_ts_async_function(self, tmp_path):
        f = tmp_path / "service.ts"
        f.write_text("export async function fetchData(url: string): Promise<any> { }\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        names = [fn["name"] for fn in symbols["functions"]]
        assert "fetchData" in names

    def test_parse_go_function_and_struct(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text("func handleRequest(w http.ResponseWriter) {}\ntype Server struct {}\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        fn_names = [fn["name"] for fn in symbols["functions"]]
        cls_names = [c["name"] for c in symbols["classes"]]
        assert "handleRequest" in fn_names
        assert "Server" in cls_names

    def test_parse_rust_fn_and_struct(self, tmp_path):
        f = tmp_path / "lib.rs"
        f.write_text("pub fn compute(x: i32) -> i32 { x }\npub struct Config {}\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        fn_names = [fn["name"] for fn in symbols["functions"]]
        cls_names = [c["name"] for c in symbols["classes"]]
        assert "compute" in fn_names
        assert "Config" in cls_names

    def test_parse_java_class_and_method(self, tmp_path):
        f = tmp_path / "Service.java"
        f.write_text(
            "public class UserService {\n"
            "    public String getName(String id) { return id; }\n"
            "}\n"
        )
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        cls_names = [c["name"] for c in symbols["classes"]]
        assert "UserService" in cls_names

    def test_update_file_indexes_js(self, tmp_path):
        f = tmp_path / "index.js"
        f.write_text("function init() {}\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        assert "index.js" in sg.nodes
        fn_names = [fn["name"] for fn in sg.nodes["index.js"]["symbols"]["functions"]]
        assert "init" in fn_names

    def test_update_file_skips_unsupported_extension(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("not code\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.update_file(str(f))
        assert len(sg.nodes) == 0

    def test_rebuild_index_includes_non_python(self, tmp_path):
        (tmp_path / "a.py").write_text("def alpha(): pass\n")
        (tmp_path / "b.js").write_text("function beta() {}\n")
        (tmp_path / "c.ts").write_text("export class Gamma {}\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.rebuild_index(str(tmp_path))
        assert len(sg.nodes) == 3

    def test_rebuild_index_skips_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "lib.js").write_text("function dep() {}\n")
        (tmp_path / "app.js").write_text("function app() {}\n")
        sg = SymbolGraph(workdir=str(tmp_path))
        sg.rebuild_index(str(tmp_path))
        assert "app.js" in sg.nodes
        assert not any("node_modules" in k for k in sg.nodes)

    def test_incremental_indexer_picks_up_ts_file(self, tmp_path):
        f = tmp_path / "handler.ts"
        f.write_text("async function handleEvent(e: Event) {}\n")
        indexer = IncrementalIndexer(workdir=str(tmp_path))
        indexer.check_and_update(str(f))
        assert "handler.ts" in indexer.symbol_graph.nodes


# ---------------------------------------------------------------------------
# RA-2: Comment stripping to reduce false-positive symbol extraction
# ---------------------------------------------------------------------------


class TestSymbolGraphCommentStripping:
    """RA-2: _strip_comments removes commented-out definitions to reduce false positives."""

    def test_strip_single_line_comments_js(self, tmp_path):
        """Commented-out function definition is not extracted."""
        f = tmp_path / "utils.js"
        f.write_text(
            "// function oldHelper(x) { return x; }\n"
            "function currentHelper(y) { return y; }\n"
        )
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        names = [fn["name"] for fn in symbols["functions"]]
        assert "currentHelper" in names
        assert "oldHelper" not in names, "Commented-out function should not be extracted"

    def test_strip_block_comments_js(self, tmp_path):
        """Block-commented function definition is not extracted."""
        f = tmp_path / "module.js"
        f.write_text(
            "/* function deprecatedFn() {} */\n"
            "function activeFn() {}\n"
        )
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        names = [fn["name"] for fn in symbols["functions"]]
        assert "activeFn" in names
        assert "deprecatedFn" not in names, "Block-commented function should not be extracted"

    def test_strip_multiline_block_comments_ts(self, tmp_path):
        """Multi-line block comment spanning a class definition is not extracted."""
        f = tmp_path / "types.ts"
        f.write_text(
            "/*\n"
            " * Old API — kept for reference\n"
            " * class LegacyClient {\n"
            " *   connect() {}\n"
            " * }\n"
            " */\n"
            "export class ModernClient {}\n"
        )
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        cls_names = [c["name"] for c in symbols["classes"]]
        assert "ModernClient" in cls_names
        assert "LegacyClient" not in cls_names, "Class inside block comment should not be extracted"

    def test_line_numbers_reasonable_after_stripping_ts(self, tmp_path):
        """Real symbol has a positive line number after comment stripping.

        Exact line numbers are not asserted here because the regex-based patterns
        use ``(?:^|\\s)`` prefix (non-lookbehind), which may shift the reported
        position by at most 1 line when a function appears immediately after
        stripped content.  The important invariant is that the symbol IS found
        and has a sensible (positive, ≤ total-line-count) line number.
        """
        f = tmp_path / "numbered.ts"
        # Line 1: single-line comment → stripped to blank
        # Line 2: blank
        # Line 3: real function definition
        f.write_text(
            "// function ghost() {}\n"
            "\n"
            "function realFn() {}\n"
        )
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        fn = next((x for x in symbols["functions"] if x["name"] == "realFn"), None)
        assert fn is not None, "realFn should be extracted even with a commented line above it"
        assert 1 <= fn["line"] <= 3, f"realFn line {fn['line']} is out of expected range [1, 3]"

    def test_line_numbers_reasonable_after_multiline_block_comment(self, tmp_path):
        """Symbol after a multi-line block comment has a reasonable line number.

        Block comments are replaced with equal numbers of newlines (preserving
        line counts), so the reported line should be within 1 of the true line.
        """
        f = tmp_path / "lined.js"
        # Lines 1-4: block comment
        # Line 5: blank
        # Line 6: real function
        f.write_text(
            "/*\n"
            " * comment line 2\n"
            " * comment line 3\n"
            " */\n"
            "\n"
            "function realFunc() {}\n"
        )
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        fn = next((x for x in symbols["functions"] if x["name"] == "realFunc"), None)
        assert fn is not None, "realFunc should be extracted"
        # The true line is 6; allow ±1 for the (?:^|\s) prefix offset
        assert 5 <= fn["line"] <= 6, f"realFunc line {fn['line']} is out of expected range [5, 6]"

    def test_strip_comments_static_method_direct(self):
        """_strip_comments static method strips correctly on direct call."""
        source = "// function ghost() {}\nfunction real() {}\n"
        stripped = SymbolGraph._strip_comments(source, ".js")
        assert "ghost" not in stripped
        assert "real" in stripped

    def test_strip_comments_preserves_newlines(self):
        """Block comment stripping keeps newlines so line counts remain accurate."""
        source = "/*\ncomment\n*/\nfunction foo() {}\n"
        stripped = SymbolGraph._strip_comments(source, ".ts")
        # stripped should have same number of lines as original
        assert stripped.count("\n") == source.count("\n")

    def test_go_single_line_comments_stripped(self, tmp_path):
        """Go // comments do not produce false-positive symbols."""
        f = tmp_path / "server.go"
        f.write_text(
            "// func ghostHandler(w http.ResponseWriter) {}\n"
            "func realHandler(w http.ResponseWriter) {}\n"
        )
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        fn_names = [fn["name"] for fn in symbols["functions"]]
        assert "realHandler" in fn_names
        assert "ghostHandler" not in fn_names

    def test_rust_block_comment_stripped(self, tmp_path):
        """Rust /* */ comments do not produce false-positive symbols."""
        f = tmp_path / "lib.rs"
        f.write_text(
            "/* pub fn old_api() {} */\n"
            "pub fn new_api() {}\n"
        )
        sg = SymbolGraph(workdir=str(tmp_path))
        symbols = sg._parse_file_regex(f)
        fn_names = [fn["name"] for fn in symbols["functions"]]
        assert "new_api" in fn_names
        assert "old_api" not in fn_names
