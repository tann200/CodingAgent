"""Tests for src/tools/repo_analysis_tools.py — AST-based repo analysis."""

import json
import pytest
from pathlib import Path

from src.tools.repo_analysis_tools import analyze_repository, _analyze_file


class TestAnalyzeFile:
    """Unit tests for _analyze_file()."""

    def test_detects_sync_functions(self, tmp_path):
        """_analyze_file detects regular def functions."""
        f = tmp_path / "mod.py"
        f.write_text("def foo(): pass\ndef bar(): pass\n")
        summary, imports = _analyze_file(f)
        assert "foo" in summary["functions"]
        assert "bar" in summary["functions"]

    def test_detects_async_functions(self, tmp_path):
        """_analyze_file detects async def functions (Task #20 / AsyncFunctionDef fix)."""
        f = tmp_path / "amod.py"
        f.write_text("async def fetch(): pass\nasync def send(): pass\n")
        summary, imports = _analyze_file(f)
        assert "fetch" in summary["functions"], "async def not detected"
        assert "send" in summary["functions"], "async def not detected"

    def test_detects_mixed_sync_async(self, tmp_path):
        """_analyze_file detects both sync and async functions in same file."""
        f = tmp_path / "mixed.py"
        f.write_text("def sync_fn(): pass\nasync def async_fn(): pass\n")
        summary, imports = _analyze_file(f)
        assert "sync_fn" in summary["functions"]
        assert "async_fn" in summary["functions"]

    def test_detects_classes(self, tmp_path):
        """_analyze_file detects class definitions."""
        f = tmp_path / "classes.py"
        f.write_text("class Foo:\n    pass\nclass Bar:\n    pass\n")
        summary, imports = _analyze_file(f)
        assert "Foo" in summary["classes"]
        assert "Bar" in summary["classes"]

    def test_detects_imports(self, tmp_path):
        """_analyze_file detects import statements."""
        f = tmp_path / "imps.py"
        f.write_text("import os\nimport sys\nfrom pathlib import Path\n")
        summary, imports = _analyze_file(f)
        assert "os" in imports
        assert "sys" in imports
        assert "pathlib" in imports

    def test_handles_syntax_error(self, tmp_path):
        """_analyze_file returns empty summary for unparseable files."""
        f = tmp_path / "bad.py"
        f.write_text("def broken(:\n    pass\n")
        summary, imports = _analyze_file(f)
        assert summary["functions"] == []
        assert summary["classes"] == []
        assert imports == []

    def test_nested_async_function(self, tmp_path):
        """_analyze_file detects async defs nested inside classes."""
        f = tmp_path / "nested.py"
        f.write_text(
            "class MyService:\n"
            "    async def handle(self): pass\n"
            "    def sync_method(self): pass\n"
        )
        summary, imports = _analyze_file(f)
        assert "handle" in summary["functions"]
        assert "sync_method" in summary["functions"]
        assert "MyService" in summary["classes"]


class TestAnalyzeRepository:
    """Integration tests for analyze_repository()."""

    def test_creates_repo_memory_json(self, tmp_path):
        """analyze_repository creates .agent-context/repo_memory.json."""
        (tmp_path / "main.py").write_text("def main(): pass\n")
        result = analyze_repository(str(tmp_path))
        assert result["status"] == "ok"
        assert (tmp_path / ".agent-context" / "repo_memory.json").exists()

    def test_memory_json_structure(self, tmp_path):
        """repo_memory.json has module_summaries and dependency_relationships."""
        (tmp_path / "a.py").write_text("import os\ndef foo(): pass\n")
        analyze_repository(str(tmp_path))
        raw = json.loads((tmp_path / ".agent-context" / "repo_memory.json").read_text())
        assert "module_summaries" in raw
        assert "dependency_relationships" in raw
        assert "a.py" in raw["module_summaries"]
        assert "foo" in raw["module_summaries"]["a.py"]["functions"]

    def test_excludes_venv_dirs(self, tmp_path):
        """analyze_repository excludes .venv and __pycache__ directories."""
        venv_dir = tmp_path / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "ignored.py").write_text("def should_be_ignored(): pass\n")
        (tmp_path / "main.py").write_text("def real(): pass\n")
        analyze_repository(str(tmp_path))
        raw = json.loads((tmp_path / ".agent-context" / "repo_memory.json").read_text())
        keys = list(raw["module_summaries"].keys())
        assert not any(".venv" in k for k in keys)
        assert any("main.py" in k for k in keys)

    def test_async_functions_in_repo_memory(self, tmp_path):
        """async def functions appear in repo_memory.json module_summaries."""
        (tmp_path / "service.py").write_text(
            "async def start(): pass\nasync def stop(): pass\n"
        )
        analyze_repository(str(tmp_path))
        raw = json.loads((tmp_path / ".agent-context" / "repo_memory.json").read_text())
        fns = raw["module_summaries"]["service.py"]["functions"]
        assert "start" in fns
        assert "stop" in fns

    def test_returns_error_on_bad_workdir(self):
        """analyze_repository returns error dict for non-existent workdir."""
        result = analyze_repository("/nonexistent/path/does/not/exist")
        assert result["status"] == "error"
        assert "error" in result
