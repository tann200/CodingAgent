"""Unit tests for src/core/context/instruction_files.py (CP-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.context.instruction_files import (
    MAX_INSTRUCTION_FILE_CHARS,
    MAX_TOTAL_INSTRUCTION_CHARS,
    InstructionFile,
    _dedupe,
    _describe_file,
    _normalize_content,
    _stable_content_hash,
    _truncate_content,
    discover_instruction_files,
    render_instruction_files,
)


# ---------------------------------------------------------------------------
# _normalize_content
# ---------------------------------------------------------------------------


class TestNormalizeContent:
    def test_collapses_triple_blank_lines(self):
        text = "a\n\n\n\nb"
        assert "\n\n\n" not in _normalize_content(text)

    def test_strips_outer_whitespace(self):
        assert _normalize_content("  hello  ") == "hello"

    def test_preserves_single_blank_line(self):
        result = _normalize_content("a\n\nb")
        assert result == "a\n\nb"


# ---------------------------------------------------------------------------
# _stable_content_hash
# ---------------------------------------------------------------------------


class TestStableContentHash:
    def test_same_content_same_hash(self):
        assert _stable_content_hash("hello") == _stable_content_hash("hello")

    def test_different_content_different_hash(self):
        assert _stable_content_hash("hello") != _stable_content_hash("world")

    def test_deterministic_across_calls(self):
        h1 = _stable_content_hash("test content")
        h2 = _stable_content_hash("test content")
        assert h1 == h2


# ---------------------------------------------------------------------------
# _truncate_content
# ---------------------------------------------------------------------------


class TestTruncateContent:
    def test_no_truncation_below_limit(self):
        text = "x" * 100
        result = _truncate_content(text, 1000)
        assert result == text.strip()

    def test_truncates_at_per_file_limit(self):
        text = "x" * (MAX_INSTRUCTION_FILE_CHARS + 100)
        result = _truncate_content(text, MAX_TOTAL_INSTRUCTION_CHARS)
        assert len(result) <= MAX_INSTRUCTION_FILE_CHARS + len("\n\n[truncated]")
        assert "[truncated]" in result

    def test_truncates_at_remaining_limit(self):
        text = "x" * 500
        result = _truncate_content(text, 200)
        assert len(result) <= 200 + len("\n\n[truncated]")
        assert "[truncated]" in result

    def test_exact_limit_no_truncation(self):
        text = "x" * MAX_INSTRUCTION_FILE_CHARS
        result = _truncate_content(text, MAX_TOTAL_INSTRUCTION_CHARS)
        assert "[truncated]" not in result


# ---------------------------------------------------------------------------
# _dedupe
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_removes_exact_duplicates(self):
        f1 = InstructionFile(Path("/a/AGENTS.md"), "same content")
        f2 = InstructionFile(Path("/b/AGENTS.md"), "same content")
        result = _dedupe([f1, f2])
        assert len(result) == 1

    def test_keeps_different_content(self):
        f1 = InstructionFile(Path("/a/AGENTS.md"), "content A")
        f2 = InstructionFile(Path("/b/AGENTS.md"), "content B")
        result = _dedupe([f1, f2])
        assert len(result) == 2

    def test_dedupes_normalized_whitespace(self):
        f1 = InstructionFile(Path("/a/AGENTS.md"), "content\n\n\nmore")
        f2 = InstructionFile(Path("/b/AGENTS.md"), "content\n\nmore")
        result = _dedupe([f1, f2])
        assert len(result) == 1

    def test_preserves_order(self):
        f1 = InstructionFile(Path("/a/AGENTS.md"), "AAA")
        f2 = InstructionFile(Path("/b/AGENTS.md"), "BBB")
        result = _dedupe([f1, f2])
        assert result[0].content == "AAA"
        assert result[1].content == "BBB"


# ---------------------------------------------------------------------------
# discover_instruction_files
# ---------------------------------------------------------------------------


class TestDiscoverInstructionFiles:
    def test_finds_agents_md_in_workdir(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Instructions\nDo something.")
        files = discover_instruction_files(tmp_path)
        assert any(f.path.name == "AGENTS.md" for f in files)

    def test_finds_agents_local_md(self, tmp_path):
        (tmp_path / "AGENTS.local.md").write_text("Local override instructions.")
        files = discover_instruction_files(tmp_path)
        assert any(f.path.name == "AGENTS.local.md" for f in files)

    def test_finds_dotagent_agents_md(self, tmp_path):
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "AGENTS.md").write_text("Dot-agent instructions.")
        files = discover_instruction_files(tmp_path)
        assert any(f.path == tmp_path / ".agent" / "AGENTS.md" for f in files)

    def test_finds_dotagent_instructions_md(self, tmp_path):
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "instructions.md").write_text("Generic instructions.")
        files = discover_instruction_files(tmp_path)
        assert any(f.path.name == "instructions.md" for f in files)

    def test_discovers_ancestor_files(self, tmp_path):
        # Create root-level and subdir-level instruction files
        sub = tmp_path / "project" / "src"
        sub.mkdir(parents=True)
        (tmp_path / "AGENTS.md").write_text("Root instructions.")
        (sub / "AGENTS.md").write_text("Subdir instructions.")
        files = discover_instruction_files(sub)
        paths = [f.path for f in files]
        assert any(p == tmp_path / "AGENTS.md" for p in paths)
        assert any(p == sub / "AGENTS.md" for p in paths)

    def test_ancestor_comes_before_workdir(self, tmp_path):
        """Root AGENTS.md should appear before the workdir AGENTS.md."""
        sub = tmp_path / "project"
        sub.mkdir()
        (tmp_path / "AGENTS.md").write_text("Root.")
        (sub / "AGENTS.md").write_text("Project.")
        files = discover_instruction_files(sub)
        paths = [f.path for f in files]
        root_idx = next(i for i, p in enumerate(paths) if p == tmp_path / "AGENTS.md")
        sub_idx = next(i for i, p in enumerate(paths) if p == sub / "AGENTS.md")
        assert root_idx < sub_idx

    def test_empty_files_ignored(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("   \n  ")
        files = discover_instruction_files(tmp_path)
        assert not any(f.path.name == "AGENTS.md" for f in files)

    def test_missing_files_silently_skipped(self, tmp_path):
        files = discover_instruction_files(tmp_path)
        assert files == []

    def test_deduplication_applied(self, tmp_path):
        sub = tmp_path / "project"
        sub.mkdir()
        content = "Identical instructions."
        (tmp_path / "AGENTS.md").write_text(content)
        (sub / "AGENTS.md").write_text(content)
        files = discover_instruction_files(sub)
        assert len([f for f in files if f.content.strip() == content.strip()]) == 1


# ---------------------------------------------------------------------------
# render_instruction_files
# ---------------------------------------------------------------------------


class TestRenderInstructionFiles:
    def test_empty_list_returns_empty_string(self):
        assert render_instruction_files([]) == ""

    def test_contains_project_instructions_header(self):
        f = InstructionFile(Path("/p/AGENTS.md"), "Do the thing.")
        result = render_instruction_files([f])
        assert "# Project instructions" in result

    def test_contains_file_label(self):
        f = InstructionFile(Path("/p/AGENTS.md"), "Do the thing.")
        result = render_instruction_files([f])
        assert "AGENTS.md" in result

    def test_contains_file_content(self):
        f = InstructionFile(Path("/p/AGENTS.md"), "Do the thing.")
        result = render_instruction_files([f])
        assert "Do the thing." in result

    def test_total_budget_enforced(self):
        # Many files, each with content right at the per-file limit
        files = [
            InstructionFile(Path(f"/p/file{i}.md"), "x" * MAX_INSTRUCTION_FILE_CHARS)
            for i in range(20)
        ]
        result = render_instruction_files(files)
        # Total chars from file content should not exceed budget + overhead
        content_chars = sum(len(f.content) for f in files if f.content in result)
        assert len(result) < MAX_TOTAL_INSTRUCTION_CHARS + 2000  # overhead for headers

    def test_truncation_note_when_budget_exceeded(self):
        files = [
            InstructionFile(Path(f"/p/file{i}.md"), "y" * MAX_INSTRUCTION_FILE_CHARS)
            for i in range(20)
        ]
        result = render_instruction_files(files)
        assert "omitted" in result

    def test_per_file_truncation(self):
        big = "z" * (MAX_INSTRUCTION_FILE_CHARS * 2)
        f = InstructionFile(Path("/p/big.md"), big)
        result = render_instruction_files([f])
        assert "[truncated]" in result

    def test_multiple_files_rendered(self):
        f1 = InstructionFile(Path("/root/AGENTS.md"), "Root instructions.")
        f2 = InstructionFile(Path("/root/proj/AGENTS.md"), "Project instructions.")
        result = render_instruction_files([f1, f2])
        assert "Root instructions." in result
        assert "Project instructions." in result


# ---------------------------------------------------------------------------
# context_builder integration smoke test
# ---------------------------------------------------------------------------


class TestContextBuilderInstructionInjection:
    def test_instruction_block_present_in_system_prompt(self, tmp_path):
        """The system prompt should contain <project_instructions> when AGENTS.md exists."""
        (tmp_path / "AGENTS.md").write_text("# Agent rules\nAlways test your code.")
        from src.core.context.context_builder import ContextBuilder

        builder = ContextBuilder(working_dir=str(tmp_path))
        messages = builder.build_prompt(
            role_name="operational",
            active_skills=[],
            task_description="test task",
            tools=[],
            conversation=[],
        )
        system_content = messages[0]["content"] if messages else ""
        assert "<project_instructions>" in system_content
        assert "Always test your code." in system_content

    def test_no_instruction_block_when_no_agents_md(self, tmp_path):
        from src.core.context.context_builder import ContextBuilder

        builder = ContextBuilder(working_dir=str(tmp_path))
        messages = builder.build_prompt(
            role_name="operational",
            active_skills=[],
            task_description="test task",
            tools=[],
            conversation=[],
        )
        system_content = messages[0]["content"] if messages else ""
        assert "<project_instructions>" not in system_content
