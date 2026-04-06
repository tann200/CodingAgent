"""test_lsp_tools.py — Unit tests for S2-A (LSP client) and S2-B (LSP tools).

All tests use ``_DummyLSPClient`` — no live language server required.
"""

from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock


# ── S2-A: LSP data classes ────────────────────────────────────────────────────

class TestLSPDataClasses:
    def test_diagnostic_severity_label(self):
        from src.core.indexing.lsp_client import Diagnostic

        assert Diagnostic(1, 0, 0, "err").severity_label == "error"
        assert Diagnostic(2, 0, 0, "warn").severity_label == "warning"
        assert Diagnostic(3, 0, 0, "info").severity_label == "info"
        assert Diagnostic(4, 0, 0, "hint").severity_label == "hint"
        assert Diagnostic(99, 0, 0, "?").severity_label == "unknown"

    def test_document_symbol_kind_label(self):
        from src.core.indexing.lsp_client import DocumentSymbol

        sym = DocumentSymbol(name="MyClass", kind=5, start_line=0, end_line=10)
        assert sym.kind_label == "Class"

        fn = DocumentSymbol(name="my_func", kind=12, start_line=1, end_line=5)
        assert fn.kind_label == "Function"

    def test_workspace_edit_defaults(self):
        from src.core.indexing.lsp_client import WorkspaceEdit

        edit = WorkspaceEdit()
        assert edit.changes == {}

    def test_location_fields(self):
        from src.core.indexing.lsp_client import Location

        loc = Location(uri="file:///x.py", start_line=3, start_col=0, end_line=3, end_col=10)
        assert loc.uri == "file:///x.py"
        assert loc.start_line == 3


# ── S2-A: _DummyLSPClient ─────────────────────────────────────────────────────

class TestDummyLSPClient:
    @pytest.mark.asyncio
    async def test_start_is_noop(self):
        from src.core.indexing.lsp_client import _DummyLSPClient

        client = _DummyLSPClient()
        await client.start()  # should not raise

    @pytest.mark.asyncio
    async def test_get_diagnostics_returns_empty(self):
        from src.core.indexing.lsp_client import _DummyLSPClient

        client = _DummyLSPClient()
        result = await client.get_diagnostics("file:///foo.py")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_hover_returns_empty_string(self):
        from src.core.indexing.lsp_client import _DummyLSPClient

        client = _DummyLSPClient()
        result = await client.get_hover("file:///foo.py", 0, 0)
        assert result == ""

    @pytest.mark.asyncio
    async def test_get_references_returns_empty(self):
        from src.core.indexing.lsp_client import _DummyLSPClient

        client = _DummyLSPClient()
        result = await client.get_references("file:///foo.py", 0, 0)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_definition_returns_empty(self):
        from src.core.indexing.lsp_client import _DummyLSPClient

        client = _DummyLSPClient()
        result = await client.get_definition("file:///foo.py", 0, 0)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_symbols_returns_empty(self):
        from src.core.indexing.lsp_client import _DummyLSPClient

        client = _DummyLSPClient()
        result = await client.get_symbols("file:///foo.py")
        assert result == []

    @pytest.mark.asyncio
    async def test_rename_returns_empty_edit(self):
        from src.core.indexing.lsp_client import _DummyLSPClient, WorkspaceEdit

        client = _DummyLSPClient()
        result = await client.rename("file:///foo.py", 0, 0, "new_name")
        assert isinstance(result, WorkspaceEdit)
        assert result.changes == {}

    def test_available_is_false(self):
        from src.core.indexing.lsp_client import _DummyLSPClient

        assert _DummyLSPClient().available is False

    @pytest.mark.asyncio
    async def test_shutdown_is_noop(self):
        from src.core.indexing.lsp_client import _DummyLSPClient

        client = _DummyLSPClient()
        await client.shutdown()  # should not raise


# ── S2-A: LSPClient parsers ───────────────────────────────────────────────────

class TestLSPClientParsers:
    def test_parse_diagnostic(self):
        from src.core.indexing.lsp_client import LSPClient

        raw = {
            "severity": 1,
            "range": {"start": {"line": 2, "character": 4}},
            "message": "Undefined name 'foo'",
            "source": "pyright",
            "code": "reportUndefinedVariable",
        }
        d = LSPClient._parse_diagnostic(raw)
        assert d.severity == 1
        assert d.line == 2
        assert d.col == 4
        assert "foo" in d.message
        assert d.source == "pyright"

    def test_parse_location(self):
        from src.core.indexing.lsp_client import LSPClient

        raw = {
            "uri": "file:///src/main.py",
            "range": {
                "start": {"line": 10, "character": 4},
                "end": {"line": 10, "character": 12},
            },
        }
        loc = LSPClient._parse_location(raw)
        assert loc.uri == "file:///src/main.py"
        assert loc.start_line == 10
        assert loc.end_col == 12

    def test_parse_symbol(self):
        from src.core.indexing.lsp_client import LSPClient

        raw = {
            "name": "MyClass",
            "kind": 5,
            "location": {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 20, "character": 0},
                }
            },
            "detail": "class MyClass",
        }
        sym = LSPClient._parse_symbol(raw)
        assert sym.name == "MyClass"
        assert sym.kind == 5
        assert sym.start_line == 0
        assert sym.end_line == 20


# ── S2-A: LSPManager ─────────────────────────────────────────────────────────

class TestLSPManager:
    @pytest.mark.asyncio
    async def test_get_client_returns_dummy_for_unknown_lang(self, tmp_path):
        from src.core.indexing.lsp_manager import LSPManager
        from src.core.indexing.lsp_client import _DummyLSPClient

        mgr = LSPManager(workspace=tmp_path)
        client = await mgr.get_client("brainfuck")
        assert isinstance(client, _DummyLSPClient)

    @pytest.mark.asyncio
    async def test_get_client_for_file_python(self, tmp_path):
        from src.core.indexing.lsp_manager import LSPManager

        mgr = LSPManager(workspace=tmp_path)
        # pylsp is likely not installed in test env — should return DummyLSPClient
        client = await mgr.get_client_for_file("main.py")
        assert client is not None

    @pytest.mark.asyncio
    async def test_get_client_for_file_unknown_ext(self, tmp_path):
        from src.core.indexing.lsp_manager import LSPManager
        from src.core.indexing.lsp_client import _DummyLSPClient

        mgr = LSPManager(workspace=tmp_path)
        client = await mgr.get_client_for_file("file.xyz")
        assert isinstance(client, _DummyLSPClient)

    def test_language_for_file(self):
        from src.core.indexing.lsp_manager import LSPManager

        assert LSPManager.language_for_file("foo.py") == "python"
        assert LSPManager.language_for_file("bar.ts") == "typescript"
        assert LSPManager.language_for_file("baz.go") == "go"
        assert LSPManager.language_for_file("unknown.xyz") == ""

    @pytest.mark.asyncio
    async def test_caching_returns_same_client(self, tmp_path):
        from src.core.indexing.lsp_manager import LSPManager

        mgr = LSPManager(workspace=tmp_path)
        c1 = await mgr.get_client("python")
        c2 = await mgr.get_client("python")
        assert c1 is c2  # cached

    @pytest.mark.asyncio
    async def test_shutdown_all(self, tmp_path):
        from src.core.indexing.lsp_manager import LSPManager

        mgr = LSPManager(workspace=tmp_path)
        await mgr.get_client("python")
        await mgr.shutdown_all()
        assert len(mgr._clients) == 0


# ── S2-B: LSP tools with DummyLSPClient ──────────────────────────────────────

class TestLSPToolsWithDummy:
    """All tools should degrade gracefully via DummyLSPClient."""

    @pytest.mark.asyncio
    async def test_lsp_diagnostics_unavailable(self, tmp_path):
        from src.tools.lsp_tools import lsp_diagnostics

        result = await lsp_diagnostics("main.py", working_dir=str(tmp_path))
        assert result["ok"] is True
        assert "unavailable" in result["output"].lower() or "clean" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_lsp_references_unavailable(self, tmp_path):
        from src.tools.lsp_tools import lsp_references

        result = await lsp_references("main.py", 0, 0, working_dir=str(tmp_path))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_lsp_definition_unavailable(self, tmp_path):
        from src.tools.lsp_tools import lsp_definition

        result = await lsp_definition("main.py", 0, 0, working_dir=str(tmp_path))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_lsp_symbols_unavailable(self, tmp_path):
        from src.tools.lsp_tools import lsp_symbols

        result = await lsp_symbols("main.py", working_dir=str(tmp_path))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_lsp_hover_unavailable(self, tmp_path):
        from src.tools.lsp_tools import lsp_hover

        result = await lsp_hover("main.py", 0, 0, working_dir=str(tmp_path))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_lsp_rename_unavailable(self, tmp_path):
        from src.tools.lsp_tools import lsp_rename

        result = await lsp_rename("main.py", 0, 0, "new_name", working_dir=str(tmp_path))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_tools_never_raise_on_bad_path(self, tmp_path):
        """All tools must return a dict even for non-existent paths."""
        from src.tools.lsp_tools import (
            lsp_diagnostics, lsp_references, lsp_definition,
            lsp_symbols, lsp_hover, lsp_rename,
        )

        bad_path = "/nonexistent/path/file.py"
        results = await asyncio.gather(
            lsp_diagnostics(bad_path, working_dir=str(tmp_path)),
            lsp_references(bad_path, 0, 0, working_dir=str(tmp_path)),
            lsp_definition(bad_path, 0, 0, working_dir=str(tmp_path)),
            lsp_symbols(bad_path, working_dir=str(tmp_path)),
            lsp_hover(bad_path, 0, 0, working_dir=str(tmp_path)),
            lsp_rename(bad_path, 0, 0, "x", working_dir=str(tmp_path)),
        )
        for r in results:
            assert isinstance(r, dict)
            assert "ok" in r


# ── S2-B: LSP tool schemas ────────────────────────────────────────────────────

class TestLSPToolSchemas:
    def test_all_schemas_have_required_fields(self):
        from src.tools.lsp_tools import LSP_TOOL_SCHEMAS

        for schema in LSP_TOOL_SCHEMAS:
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema
            assert "fn" in schema
            assert callable(schema["fn"])

    def test_schema_names_unique(self):
        from src.tools.lsp_tools import LSP_TOOL_SCHEMAS

        names = [s["name"] for s in LSP_TOOL_SCHEMAS]
        assert len(names) == len(set(names))

    def test_expected_tools_present(self):
        from src.tools.lsp_tools import LSP_TOOL_SCHEMAS

        names = {s["name"] for s in LSP_TOOL_SCHEMAS}
        assert "lsp_diagnostics" in names
        assert "lsp_references" in names
        assert "lsp_definition" in names
        assert "lsp_symbols" in names
        assert "lsp_hover" in names
        assert "lsp_rename" in names


# ── S2-C: Formatter ───────────────────────────────────────────────────────────

class TestFormatter:
    def test_run_formatter_returns_false_for_missing_binary(self, tmp_path):
        """When binary not installed, run_formatter returns False without raising."""
        from src.tools.formatter import run_formatter, _CMD_CACHE

        # Use a non-existent binary name via a temp YAML override
        with patch("src.tools.formatter._get_config") as mock_cfg:
            mock_cfg.return_value = {
                ".testfmt": {"cmd": ["__nonexistent_binary_xyz__", "{file}"]}
            }
            _CMD_CACHE.clear()
            test_file = tmp_path / "foo.testfmt"
            test_file.write_text("hello", encoding="utf-8")
            result = run_formatter(str(test_file))
            assert result is False

    def test_run_formatter_no_config_for_extension(self, tmp_path):
        """Unknown extension returns False without error."""
        from src.tools.formatter import run_formatter, _CMD_CACHE

        _CMD_CACHE.clear()
        f = tmp_path / "file.zzz"
        f.write_text("x", encoding="utf-8")
        result = run_formatter(str(f))
        assert result is False

    def test_run_formatter_caches_unavailable(self, tmp_path):
        """Second call for same unavailable binary uses cache (no subprocess)."""
        from src.tools.formatter import run_formatter, _CMD_CACHE

        _CMD_CACHE.clear()
        f = tmp_path / "a.zzz"
        f.write_text("x", encoding="utf-8")
        run_formatter(str(f))
        with patch("subprocess.run") as mock_run:
            run_formatter(str(f))
            mock_run.assert_not_called()  # cache hit — no subprocess

    def test_write_file_result_includes_formatted_key(self, tmp_path):
        """write_file returns 'formatted: True' when formatter ran successfully."""
        import sys

        sys.path.insert(0, str(tmp_path))
        with patch("src.tools.formatter.run_formatter", return_value=True) as mock_fmt:
            from src.tools.file_tools import write_file

            f = tmp_path / "test.py"
            result = write_file(str(f), "x = 1\n", workdir=tmp_path)
            # formatter was called
            mock_fmt.assert_called_once_with(str(f.resolve()))
            assert result.get("formatted") is True
