"""Unit tests for ``codingagent skill`` CLI subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cli import skill_cmd
from src.main import _parse_args


# --------------------------------------------------------------------------
# Parser Tests
# --------------------------------------------------------------------------


class TestSkillArgParser:
    def test_skill_list_defaults(self):
        args = _parse_args(["skill", "list"])
        assert args.subcommand == "skill"
        assert args.skill_action == "list"
        assert args.json is False

    def test_skill_show(self):
        args = _parse_args(["skill", "show", "polyglot-verify", "--json"])
        assert args.subcommand == "skill"
        assert args.skill_action == "show"
        assert args.name == "polyglot-verify"
        assert args.json is True

    def test_skill_export(self):
        args = _parse_args(
            ["skill", "export", "polyglot-verify", "--target", "claude", "--out", "/tmp/skills", "--json"]
        )
        assert args.subcommand == "skill"
        assert args.skill_action == "export"
        assert args.name == "polyglot-verify"
        assert args.target == "claude"
        assert args.out == "/tmp/skills"
        assert args.json is True

    def test_skill_export_all(self):
        args = _parse_args(["skill", "export", "--all", "--target", "cursor"])
        assert args.subcommand == "skill"
        assert args.skill_action == "export"
        assert args.all is True
        assert args.target == "cursor"

    def test_skill_validate_all(self):
        args = _parse_args(["skill", "validate", "--all", "--json"])
        assert args.subcommand == "skill"
        assert args.skill_action == "validate"
        assert args.all is True
        assert args.json is True


# --------------------------------------------------------------------------
# Metadata & Extraction Tests
# --------------------------------------------------------------------------


class TestSkillMetadataExtraction:
    def test_extract_yaml_frontmatter(self):
        sample = """---
name: custom-skill
description: A custom prompt skill for testing.
author: Test
---

# Instruction Title
Follow these steps.
"""
        name, desc, body = skill_cmd.extract_skill_metadata(sample, "default")
        assert name == "custom-skill"
        assert desc == "A custom prompt skill for testing."
        assert "# Instruction Title" in body

    def test_extract_plain_markdown_paragraph(self):
        sample = """# Skill Name: My Helper

This skill should be applied when debugging tests. It provides step-by-step checklists.

## Steps
1. Do this.
"""
        name, desc, body = skill_cmd.extract_skill_metadata(sample, "fallback-name")
        assert name == "fallback-name"
        assert "This skill should be applied when debugging tests." in desc
        assert "## Steps" in body


# --------------------------------------------------------------------------
# Listing & Inspection Tests
# --------------------------------------------------------------------------


class TestSkillListing:
    def test_list_all_skills_finds_bundles_and_flat(self):
        skills = skill_cmd.list_all_skills()
        assert len(skills) >= 10
        names = {s["name"] for s in skills}
        assert "polyglot-verify" in names
        assert "atomic-edit-rollback" in names
        assert "git-safe-review" in names
        assert "code_review" in names

        poly = next(s for s in skills if s["name"] == "polyglot-verify")
        assert poly["is_bundle"] is True
        assert "Multi-language" in poly["description"]

        cr = next(s for s in skills if s["name"] == "code_review")
        assert cr["is_bundle"] is False
        assert len(cr["description"]) > 10

    def test_get_skill_info_not_found(self):
        assert skill_cmd.get_skill_info("non_existent_skill_xyz") is None


# --------------------------------------------------------------------------
# Export Tests
# --------------------------------------------------------------------------


class TestSkillExport:
    def test_export_claude_target(self, tmp_path: Path):
        res = skill_cmd.export_single_skill("polyglot-verify", target="claude", out_dir=str(tmp_path))
        assert res["status"] == "exported"
        expected_file = tmp_path / ".claude" / "skills" / "polyglot-verify" / "SKILL.md"
        assert expected_file.is_file()
        content = expected_file.read_text(encoding="utf-8")
        assert "name: polyglot-verify" in content
        assert "description:" in content

    def test_export_cursor_target(self, tmp_path: Path):
        res = skill_cmd.export_single_skill("atomic-edit-rollback", target="cursor", out_dir=str(tmp_path))
        assert res["status"] == "exported"
        expected_file = tmp_path / ".cursor" / "skills" / "atomic-edit-rollback" / "SKILL.md"
        assert expected_file.is_file()

    def test_export_agensi_target(self, tmp_path: Path):
        res = skill_cmd.export_single_skill("git-safe-review", target="agensi", out_dir=str(tmp_path))
        assert res["status"] == "exported"
        expected_file = tmp_path / ".agensi" / "skills" / "git-safe-review" / "SKILL.md"
        assert expected_file.is_file()

    def test_export_not_found(self, tmp_path: Path):
        res = skill_cmd.export_single_skill("unknown_skill", target="agensi", out_dir=str(tmp_path))
        assert res["status"] == "not_found"


# --------------------------------------------------------------------------
# Validation Tests
# --------------------------------------------------------------------------


class TestSkillValidation:
    def test_validate_all_packaged_skills(self):
        skills = skill_cmd._list_skill_names()
        for name in skills:
            val = skill_cmd.validate_single_skill(name)
            assert val["valid"] is True, f"Skill '{name}' failed validation: {val['issues']}"

    def test_validate_missing_skill(self):
        val = skill_cmd.validate_single_skill("does_not_exist_xyz")
        assert val["valid"] is False
        assert any("not found" in issue.lower() for issue in val["issues"])


# --------------------------------------------------------------------------
# CLI Execution Tests
# --------------------------------------------------------------------------


class TestSkillCommandExecution:
    def test_run_skill_list_json(self, capsys):
        code = skill_cmd.run_skill(_parse_args(["skill", "list", "--json"]))
        assert code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == "ok"
        assert payload["count"] >= 10

    def test_run_skill_show(self, capsys):
        code = skill_cmd.run_skill(_parse_args(["skill", "show", "polyglot-verify"]))
        assert code == 0
        out = capsys.readouterr().out
        assert "polyglot-verify" in out
        assert "Pre-flight Syntax Check" in out

    def test_run_skill_show_not_found(self, capsys):
        code = skill_cmd.run_skill(_parse_args(["skill", "show", "unknown_skill_xyz"]))
        assert code == 1

    def test_run_skill_export_all_json(self, tmp_path: Path, capsys):
        code = skill_cmd.run_skill(
            _parse_args(["skill", "export", "--all", "--target", "agensi", "--out", str(tmp_path), "--json"])
        )
        assert code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == "ok"
        assert len(payload["exported"]) >= 10
        assert (tmp_path / ".agensi" / "skills" / "polyglot-verify" / "SKILL.md").exists()
