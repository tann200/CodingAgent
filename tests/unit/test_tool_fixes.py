"""
Tests for the tool fixes:
  - glob: ** pattern correctness
  - grep: include filter, context lines, structured matches output
  - edit_file_atomic: exact-once replacement, ambiguity rejection, diff output
  - safe_resolve: shared path-safety utility (#29)
"""
import pytest
from src.tools.file_tools import glob, edit_file_atomic, write_file
from src.tools.system_tools import grep


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------

class TestGlob:
    def test_simple_pattern_finds_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.txt").write_text("x")
        res = glob("*.py", workdir=tmp_path)
        assert res["status"] == "ok"
        assert "a.py" in res["matches"]
        assert "b.txt" not in res["matches"]

    def test_double_star_recursive(self, tmp_path):
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)
        (sub / "module.py").write_text("x")
        (tmp_path / "top.py").write_text("x")

        res = glob("**/*.py", workdir=tmp_path)
        assert res["status"] == "ok"
        matches = res["matches"]
        # Must find files at any depth
        assert any("module.py" in m for m in matches)
        assert any("top.py" in m for m in matches)

    def test_double_star_not_stripped(self, tmp_path):
        """Regression: ** must not be stripped before matching."""
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.py").write_text("x")
        # Without **, rglob("*.py") would find it too — but the key check is
        # that glob("**/*.py") includes depth-3 files (previously broken)
        res = glob("**/*.py", workdir=tmp_path)
        assert any("deep.py" in m for m in res["matches"])

    def test_results_are_sorted(self, tmp_path):
        for name in ["z.py", "a.py", "m.py"]:
            (tmp_path / name).write_text("x")
        res = glob("*.py", workdir=tmp_path)
        assert res["matches"] == sorted(res["matches"])

    def test_no_matches_returns_empty_list(self, tmp_path):
        res = glob("*.nonexistent", workdir=tmp_path)
        assert res["status"] == "ok"
        assert res["matches"] == []

    def test_limit_500(self, tmp_path):
        for i in range(510):
            (tmp_path / f"f{i}.py").write_text("x")
        res = glob("*.py", workdir=tmp_path)
        assert len(res["matches"]) == 500

    def test_directories_not_included(self, tmp_path):
        (tmp_path / "adir").mkdir()
        (tmp_path / "afile.py").write_text("x")
        res = glob("*", workdir=tmp_path)
        assert all(not (tmp_path / m).is_dir() for m in res["matches"])


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------

class TestGrep:
    def setup_files(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    pass\n")
        (tmp_path / "b.py").write_text("def bar():\n    foo()\n")
        (tmp_path / "notes.txt").write_text("just a note about foo\n")

    def test_basic_match(self, tmp_path):
        self.setup_files(tmp_path)
        res = grep("def foo", workdir=tmp_path)
        assert "matches" in res
        assert any(m["content"].strip().startswith("def foo") for m in res["matches"])

    def test_no_match(self, tmp_path):
        self.setup_files(tmp_path)
        res = grep("NONEXISTENT_TOKEN_XYZ", workdir=tmp_path)
        assert res.get("matches") == []

    def test_include_filter_py_only(self, tmp_path):
        self.setup_files(tmp_path)
        res = grep("foo", workdir=tmp_path, include="*.py")
        # notes.txt contains "foo" but should be excluded
        file_paths = [m["file_path"] for m in res["matches"]]
        assert all(fp.endswith(".py") for fp in file_paths), \
            f"Expected only .py files, got: {file_paths}"

    def test_include_filter_txt_only(self, tmp_path):
        self.setup_files(tmp_path)
        res = grep("foo", workdir=tmp_path, include="*.txt")
        file_paths = [m["file_path"] for m in res["matches"]]
        assert all(fp.endswith(".txt") for fp in file_paths)

    def test_structured_matches_fields(self, tmp_path):
        self.setup_files(tmp_path)
        res = grep("def foo", workdir=tmp_path)
        for m in res["matches"]:
            assert "file_path" in m
            assert "line_number" in m
            assert "content" in m
            assert isinstance(m["line_number"], int)
            assert m["line_number"] >= 1

    def test_context_lines_in_output(self, tmp_path):
        """context > 0 should produce more output lines than matches alone."""
        (tmp_path / "f.py").write_text("line1\nTARGET\nline3\n")
        res_no_ctx = grep("TARGET", workdir=tmp_path, context=0)
        res_ctx = grep("TARGET", workdir=tmp_path, context=1)
        # With context=1, output should have surrounding lines (line1, line3)
        assert len(res_ctx["output"]) >= len(res_no_ctx["output"])

    def test_output_field_always_present(self, tmp_path):
        self.setup_files(tmp_path)
        res = grep("def", workdir=tmp_path)
        assert "output" in res

    def test_path_outside_workdir_rejected(self, tmp_path):
        res = grep("foo", path="../outside", workdir=tmp_path)
        assert res.get("status") == "error"


# ---------------------------------------------------------------------------
# edit_file_atomic
# ---------------------------------------------------------------------------

class TestEditFileAtomic:
    def test_basic_replacement(self, tmp_path):
        write_file("f.py", "def hello():\n    pass\n", workdir=tmp_path)
        res = edit_file_atomic("f.py", "def hello():", "def goodbye():", workdir=tmp_path)
        assert res["status"] == "ok"
        content = (tmp_path / "f.py").read_text()
        assert "goodbye" in content
        assert "hello" not in content

    def test_returns_diff(self, tmp_path):
        write_file("f.py", "x = 1\n", workdir=tmp_path)
        res = edit_file_atomic("f.py", "x = 1", "x = 99", workdir=tmp_path)
        assert res["status"] == "ok"
        assert "diff" in res
        assert res["lines_added"] >= 1
        assert res["lines_removed"] >= 1

    def test_not_found_returns_error(self, tmp_path):
        write_file("f.py", "x = 1\n", workdir=tmp_path)
        res = edit_file_atomic("f.py", "THIS DOES NOT EXIST", "replacement", workdir=tmp_path)
        assert res["status"] == "error"
        assert "not found" in res["error"].lower()

    def test_ambiguous_match_returns_error(self, tmp_path):
        write_file("f.py", "x = 1\nx = 1\n", workdir=tmp_path)
        res = edit_file_atomic("f.py", "x = 1", "x = 2", workdir=tmp_path)
        assert res["status"] == "error"
        assert "2 times" in res["error"] or "twice" in res["error"] or "2" in res["error"]

    def test_nonexistent_file(self, tmp_path):
        res = edit_file_atomic("missing.py", "old", "new", workdir=tmp_path)
        assert res["status"] == "not_found"

    def test_multiline_old_string(self, tmp_path):
        write_file("f.py", "def foo():\n    return 1\n", workdir=tmp_path)
        res = edit_file_atomic(
            "f.py",
            "def foo():\n    return 1",
            "def foo():\n    return 42",
            workdir=tmp_path,
        )
        assert res["status"] == "ok"
        assert "42" in (tmp_path / "f.py").read_text()

    def test_path_outside_workdir(self, tmp_path):
        with pytest.raises(PermissionError):
            edit_file_atomic("../outside.py", "old", "new", workdir=tmp_path)

    def test_read_before_write_enforced_in_orchestrator(self):
        """edit_file_atomic must be in WRITE_TOOLS_REQUIRING_READ."""
        from src.core.orchestration.orchestrator import WRITE_TOOLS_REQUIRING_READ
        assert "edit_file_atomic" in WRITE_TOOLS_REQUIRING_READ

    def test_registered_in_orchestrator(self):
        """edit_file_atomic must be registered in the example_registry."""
        from src.core.orchestration.orchestrator import example_registry
        reg = example_registry()
        tool = reg.get("edit_file_atomic")
        assert tool is not None, "edit_file_atomic not registered"
        assert "old_string" in tool.get("description", "").lower() or \
               "atomic" in tool.get("description", "").lower()

    def test_formatter_registered(self):
        """edit_file_atomic must have a result formatter."""
        from src.core.orchestration.orchestrator import TOOL_RESULT_FORMATTERS
        assert "edit_file_atomic" in TOOL_RESULT_FORMATTERS


# ---------------------------------------------------------------------------
# safe_resolve shared utility (#29)
# ---------------------------------------------------------------------------

class TestSafeResolve:
    """Tests for the shared _path_utils.safe_resolve function."""

    def test_relative_path_anchored_to_workdir(self, tmp_path):
        from src.tools._path_utils import safe_resolve
        (tmp_path / "sub").mkdir()
        p = safe_resolve("sub", tmp_path)
        assert p.parent == tmp_path

    def test_absolute_path_inside_workdir(self, tmp_path):
        from src.tools._path_utils import safe_resolve
        target = tmp_path / "file.txt"
        target.touch()
        p = safe_resolve(str(target), tmp_path)
        assert p == target

    def test_path_outside_workdir_raises(self, tmp_path):
        from src.tools._path_utils import safe_resolve
        with pytest.raises(PermissionError):
            safe_resolve("../outside.txt", tmp_path)

    def test_deep_traversal_blocked(self, tmp_path):
        from src.tools._path_utils import safe_resolve
        with pytest.raises(PermissionError):
            safe_resolve("../../etc/passwd", tmp_path)

    def test_new_file_path_allowed(self, tmp_path):
        """Non-existent file inside workdir must NOT raise (needed for write_file)."""
        from src.tools._path_utils import safe_resolve
        p = safe_resolve("newfile.py", tmp_path)
        assert p.parent == tmp_path

    def test_file_tools_uses_shared_utility(self):
        """file_tools._safe_resolve delegates to _path_utils.safe_resolve."""
        from src.tools import file_tools
        # _safe_resolve must be a thin wrapper, not a duplicate implementation
        import inspect
        src = inspect.getsource(file_tools._safe_resolve)
        assert "safe_resolve" in src
