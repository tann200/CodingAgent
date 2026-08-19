"""Tests for the Python syntax gate in execution_helpers."""

from src.core.orchestration.graph.nodes.execution_helpers import _validate_python_syntax


def test_valid_python_passes():
    assert _validate_python_syntax("x = 1\n", "foo.py") is None


def test_valid_multiline_python_passes():
    code = "def foo(x):\n    return x + 1\n"
    assert _validate_python_syntax(code, "bar.py") is None


def test_invalid_python_blocked():
    err = _validate_python_syntax("def foo(\n", "bar.py")
    assert err is not None
    assert "syntax" in err.lower() or "Syntax" in err


def test_non_python_always_passes():
    assert _validate_python_syntax("{ not: valid python }", "foo.ts") is None
    assert _validate_python_syntax("not python", "readme.md") is None
    assert _validate_python_syntax("def foo(\n", "foo.js") is None


def test_empty_path_passes():
    # No path hint → always passes regardless of content
    assert _validate_python_syntax("def foo(\n", "") is None
