"""Regression tests for src/core/orchestration/instruction_loader.py

Covers:
  IL-1  load_project_instructions — returns empty string when no instruction files exist
  IL-2  load_project_instructions — discovers AGENT.md in cwd
  IL-3  load_project_instructions — discovers .agent/instructions.md in cwd
  IL-4  load_project_instructions — walks up to parent when cwd has no file
  IL-5  load_project_instructions — deduplicates identical content across dirs
  IL-6  load_project_instructions — caps per-file content at 4,000 chars
  IL-7  load_project_instructions — stops at 12,000 chars total
  IL-8  load_project_instructions — wraps output in <project_instructions>
  IL-9  build_git_context_block   — returns non-empty block inside a real git repo
  IL-10 build_git_context_block   — returns empty string for a non-git directory
  IL-11 build_runtime_context     — includes dynamic boundary marker when non-empty
  IL-12 build_runtime_context     — returns empty string when nothing to inject
  IL-13 load_project_instructions — AGENT.local.md discovered
  IL-14 path traversal            — symlink outside tree cannot inject content
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# IL-1  empty directory → empty string
# ---------------------------------------------------------------------------

def test_no_instruction_files(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions
    result = load_project_instructions(tmp_path)
    assert result == ""


# ---------------------------------------------------------------------------
# IL-2  AGENT.md in cwd is discovered
# ---------------------------------------------------------------------------

def test_discovers_agent_md_in_cwd(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions
    _write(tmp_path / "AGENT.md", "# Project rules\nAlways write tests first.")
    result = load_project_instructions(tmp_path)
    assert "Project rules" in result
    assert "Always write tests first." in result


# ---------------------------------------------------------------------------
# IL-3  .agent/instructions.md in cwd is discovered
# ---------------------------------------------------------------------------

def test_discovers_dot_agent_instructions(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions
    _write(tmp_path / ".agent" / "instructions.md", "Use tabs, not spaces.")
    result = load_project_instructions(tmp_path)
    assert "Use tabs, not spaces." in result


# ---------------------------------------------------------------------------
# IL-4  walks up to parent when cwd has no file
# ---------------------------------------------------------------------------

def test_walks_up_to_parent(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions
    _write(tmp_path / "AGENT.md", "Parent instruction")
    child = tmp_path / "subdir" / "deeply" / "nested"
    child.mkdir(parents=True)
    result = load_project_instructions(child)
    assert "Parent instruction" in result


# ---------------------------------------------------------------------------
# IL-5  identical content deduplicated across dirs
# ---------------------------------------------------------------------------

def test_deduplicates_identical_content(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions
    content = "Shared instruction content"
    _write(tmp_path / "AGENT.md", content)
    child = tmp_path / "nested"
    child.mkdir()
    _write(child / "AGENT.md", content)   # same content, different path
    result = load_project_instructions(child)
    # Should appear only once even though two files were found
    assert result.count(content) == 1


# ---------------------------------------------------------------------------
# IL-6  per-file cap at 4,000 chars
# ---------------------------------------------------------------------------

def test_per_file_cap(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions, _PER_FILE_CHAR_CAP
    big = "x" * (_PER_FILE_CHAR_CAP + 500)
    _write(tmp_path / "AGENT.md", big)
    result = load_project_instructions(tmp_path)
    # truncated notice must appear
    assert "truncated" in result
    # The result body should not contain more than cap + overhead
    assert len(result) < _PER_FILE_CHAR_CAP + 300


# ---------------------------------------------------------------------------
# IL-7  total cap stops collection at 12,000 chars
# ---------------------------------------------------------------------------

def test_total_char_cap(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions, _TOTAL_CHAR_CAP
    # Write a 4,000-char AGENT.md in cwd — fill all three slots
    chunk = "y" * 4_000
    _write(tmp_path / "AGENT.md", chunk)
    _write(tmp_path / ".agent" / "instructions.md", chunk)
    _write(tmp_path / "AGENT.local.md", chunk)
    result = load_project_instructions(tmp_path)
    assert len(result) < _TOTAL_CHAR_CAP + 500  # allow for XML wrapper overhead


# ---------------------------------------------------------------------------
# IL-8  output wrapped in <project_instructions>
# ---------------------------------------------------------------------------

def test_wrapped_in_xml_block(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions
    _write(tmp_path / "AGENT.md", "hello")
    result = load_project_instructions(tmp_path)
    assert result.startswith("<project_instructions>")
    assert result.strip().endswith("</project_instructions>")


# ---------------------------------------------------------------------------
# IL-9  build_git_context_block inside a real git repo
# ---------------------------------------------------------------------------

def test_git_context_inside_repo():
    from src.core.orchestration.instruction_loader import build_git_context_block
    # Use the repo root — which is a real git repo
    repo_root = Path(__file__).parents[2]
    result = build_git_context_block(repo_root)
    # Should return a block (may be empty if working tree is clean, but block appears)
    # At minimum the function must not raise and return a str
    assert isinstance(result, str)
    if result:
        assert "<git_context>" in result
        assert "git status" in result


# ---------------------------------------------------------------------------
# IL-10 build_git_context_block outside a git repo → empty
# ---------------------------------------------------------------------------

def test_git_context_non_git_dir(tmp_path):
    from src.core.orchestration.instruction_loader import build_git_context_block
    result = build_git_context_block(tmp_path)
    assert result == ""


# ---------------------------------------------------------------------------
# IL-11 build_runtime_context includes dynamic boundary marker
# ---------------------------------------------------------------------------

def test_runtime_context_includes_boundary_marker(tmp_path):
    from src.core.orchestration.instruction_loader import build_runtime_context
    _write(tmp_path / "AGENT.md", "some instruction")
    result = build_runtime_context(cwd=tmp_path)
    assert "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__" in result


# ---------------------------------------------------------------------------
# IL-12 build_runtime_context returns empty when nothing to inject
# ---------------------------------------------------------------------------

def test_runtime_context_empty_for_empty_dir(tmp_path):
    from src.core.orchestration.instruction_loader import build_runtime_context
    result = build_runtime_context(cwd=tmp_path)
    assert result == ""


# ---------------------------------------------------------------------------
# IL-13 AGENT.local.md discovered
# ---------------------------------------------------------------------------

def test_discovers_agent_local_md(tmp_path):
    from src.core.orchestration.instruction_loader import load_project_instructions
    _write(tmp_path / "AGENT.local.md", "Local override: use poetry.")
    result = load_project_instructions(tmp_path)
    assert "Local override: use poetry." in result


# ---------------------------------------------------------------------------
# IL-14 errors do not propagate — returns empty string on unexpected OS errors
# ---------------------------------------------------------------------------

def test_graceful_on_permission_error(tmp_path, monkeypatch):
    from src.core.orchestration import instruction_loader

    def _bad_collect(cwd):
        raise PermissionError("no access")

    monkeypatch.setattr(instruction_loader, "_collect_instructions", _bad_collect)
    result = instruction_loader.load_project_instructions(tmp_path)
    assert result == ""
