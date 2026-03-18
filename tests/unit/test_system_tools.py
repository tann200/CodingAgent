import pytest
import tempfile
import os
import subprocess
from pathlib import Path
from src.tools.system_tools import grep, summarize_structure


@pytest.fixture
def tmp_workdir(tmp_path):
    return tmp_path


def test_grep_finds_pattern(tmp_workdir):
    (tmp_workdir / "test.py").write_text("def foo():\n    pass\n")
    result = grep("def", path=".", workdir=tmp_workdir)
    assert "output" in result or "status" in result


def test_grep_no_match(tmp_workdir):
    (tmp_workdir / "test.py").write_text("def foo():\n    pass\n")
    result = grep("nonexistent", path=".", workdir=tmp_workdir)
    assert "output" in result


def test_grep_with_path(tmp_workdir):
    (tmp_workdir / "sub").mkdir()
    (tmp_workdir / "sub" / "file.py").write_text("def bar():\n    pass\n")
    result = grep("def", path="sub", workdir=tmp_workdir)
    assert "output" in result


def test_summarize_structure(tmp_workdir):
    (tmp_workdir / "file1.py").write_text("print('hello')")
    (tmp_workdir / "file2.txt").write_text("hello")

    result = summarize_structure(workdir=tmp_workdir)
    assert "file_count" in result
    assert result["file_count"] >= 2


def test_summarize_structure_basic(tmp_workdir):
    (tmp_workdir / "visible.py").write_text("code")

    result = summarize_structure(workdir=tmp_workdir)
    assert "file_count" in result
    assert result["file_count"] >= 1
