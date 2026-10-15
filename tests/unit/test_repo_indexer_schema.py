"""Unit tests for repo_indexer.refresh_file_in_index schema normalization (Audit Item 6c / Section 8)."""

import json
from pathlib import Path

from src.core.indexing.repo_indexer import (
    index_repository,
    refresh_file_in_index,
)


def test_refresh_file_in_index_preserves_normalized_file_entry(tmp_path: Path):
    """refresh_file_in_index must update data['files'] with a normalized dict:

    {'path': rel, 'language': lang, 'imports': ...} rather than raw AST dumps.
    """
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    py_file = repo_dir / "example.py"
    py_file.write_text("import os\n\ndef greet(name):\n    return f'Hello, {name}'\n", encoding="utf-8")

    # Build initial index
    index_data = index_repository(str(repo_dir))
    assert len(index_data["files"]) == 1
    orig_entry = index_data["files"][0]
    assert orig_entry["path"] == "example.py"
    assert orig_entry["language"] == "python"
    assert "imports" in orig_entry
    assert "classes" not in orig_entry  # Normal entries don't have raw parser dumps

    # Now edit the file and call refresh_file_in_index
    py_file.write_text("import sys\nimport os\n\ndef greet_v2(name):\n    return f'Hi, {name}'\n", encoding="utf-8")
    ok = refresh_file_in_index(py_file, str(repo_dir))
    assert ok is True

    # Read saved index
    ctx_dir = repo_dir / ".codingAgent"
    if not ctx_dir.exists():
        ctx_dir = repo_dir / ".agent-context"
    saved = json.loads((ctx_dir / "repo_index.json").read_text(encoding="utf-8"))

    assert len(saved["files"]) == 1
    refreshed_entry = saved["files"][0]
    assert refreshed_entry["path"] == "example.py"
    assert refreshed_entry["language"] == "python"
    assert "os" in refreshed_entry["imports"]
    assert "sys" in refreshed_entry["imports"]
    # Verify raw AST fields are NOT dumped into file entry
    assert "classes" not in refreshed_entry
    assert "functions" not in refreshed_entry

    # Verify top-level symbols list was refreshed
    symbol_names = [s["symbol_name"] for s in saved["symbols"] if s.get("file_path") == "example.py"]
    assert "greet_v2" in symbol_names
    assert "greet" not in symbol_names
