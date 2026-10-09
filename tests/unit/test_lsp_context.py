"""LSP symbol-context injection tests.

Regression coverage for the audit finding that ``get_lsp_context_block`` was
always empty: ``_get_symbols`` iterated ``SymbolGraph`` node symbols as if the
``classes``/``functions`` entries were dicts, but the graph stores them as
lists, so every lookup raised and the exception was swallowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.indexing import lsp_context


def _fake_graph(nodes):
    class _FakeSymbolGraph:
        def __init__(self, workdir=None):
            self.nodes = nodes

    return _FakeSymbolGraph


@pytest.fixture
def fake_graph(monkeypatch):
    nodes = {
        "pkg/mod.py": {
            "symbols": {
                "classes": [{"name": "Widget", "line": 10}],
                "functions": [{"name": "make_widget", "line": 20}],
                "imports": ["os"],
                "docstring": "module doc",
            }
        },
        "pkg/other.py": {
            "symbols": {
                "classes": [],
                "functions": [{"name": "helper", "line": 5}],
                "imports": [],
                "docstring": "",
            }
        },
    }
    monkeypatch.setattr(
        "src.core.indexing.symbol_graph.SymbolGraph", _fake_graph(nodes)
    )
    return nodes


def test_get_symbols_reads_classes_and_functions(fake_graph):
    symbols = lsp_context._get_symbols(Path("/tmp"))
    found = {(s["kind"], s["name"]) for s in symbols}
    assert ("class", "Widget") in found
    assert ("function", "make_widget") in found
    assert ("function", "helper") in found
    # imports/docstring are not symbols
    assert all(s["name"] not in ("os", "module doc") for s in symbols)


def test_get_symbols_sorted_classes_first(fake_graph):
    symbols = lsp_context._get_symbols(Path("/tmp"))
    kinds = [s["kind"] for s in symbols]
    assert kinds.index("class") < kinds.index("function")


def test_get_symbols_tolerates_bad_shapes(monkeypatch):
    nodes = {
        "bad.py": {
            "symbols": {
                "classes": [{"name": "Ok", "line": 1}, "not-a-dict", None],
                "functions": "not-a-list",
            }
        },
        "weird.py": {"symbols": None},
    }
    monkeypatch.setattr(
        "src.core.indexing.symbol_graph.SymbolGraph", _fake_graph(nodes)
    )
    symbols = lsp_context._get_symbols(Path("/tmp"))
    assert [s["name"] for s in symbols] == ["Ok"]


def test_context_block_renders_symbols(fake_graph, monkeypatch):
    monkeypatch.setenv("CODINGAGENT_LSP_CONTEXT", "1")
    block = lsp_context.get_lsp_context_block(Path("/tmp"))
    assert "<lsp_context>" in block
    assert "class Widget" in block
    assert "function make_widget" in block


def test_context_block_empty_when_disabled(fake_graph, monkeypatch):
    monkeypatch.delenv("CODINGAGENT_LSP_CONTEXT", raising=False)
    monkeypatch.setattr(lsp_context, "_is_enabled", lambda: False)
    assert lsp_context.get_lsp_context_block(Path("/tmp")) == ""
