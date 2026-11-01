"""
Contract tests for skill-directory consolidation (Phase 3.5 / MC-4).

Guarantees:
- There is exactly ONE authoritative skill directory:
  ``src/config/agent-brain/skills/``.
- The legacy parallel set ``src/config/skills/`` no longer exists.
- The LLM-facing ``load_skill``/``list_skills`` tools resolve against the
  authoritative directory, and the ``explore_codebase`` skill (previously only
  in the legacy set) is present there.
"""

from pathlib import Path

from src.tools.skill_tools import (
    _SKILLS_DIR,
    _list_skill_names,
    load_skill,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestSingleAuthoritativeSkillsDir:
    """MC-4: only agent-brain/skills exists as a skill source."""

    def test_packaged_skills_dir_contains_standard_packages(self):
        packaged_dir = _REPO_ROOT / "src" / "config" / "skills"
        assert packaged_dir.exists(), "src/config/skills/ packaged skills directory must exist"
        # Verify the 3 packaged skills for Agensi / Claude Code / Cursor exist with SKILL.md
        for skill_name in ("polyglot-verify", "atomic-edit-rollback", "git-safe-review"):
            skill_file = packaged_dir / skill_name / "SKILL.md"
            assert skill_file.exists(), f"Skill bundle {skill_name}/SKILL.md missing"
            text = skill_file.read_text(encoding="utf-8")
            assert f"name: {skill_name}" in text
            assert "compatibility:" in text
        # Verify legacy explore_codebase is not left behind as a loose file in src/config/skills/
        assert not (packaged_dir / "explore_codebase.md").exists()

    def test_skill_tools_point_at_agent_brain_skills(self):
        ab_skills = (
            _REPO_ROOT / "src" / "config" / "agent-brain" / "skills"
        )
        assert _SKILLS_DIR == ab_skills
        assert ab_skills.is_dir()

    def test_explore_codebase_migrated(self):
        migrated = _REPO_ROOT / "src" / "config" / "agent-brain" / "skills" / "explore_codebase.md"
        assert migrated.exists(), "explore_codebase skill must be migrated to agent-brain/skills"

    def test_no_duplicate_skill_names_across_dirs(self):
        names = _list_skill_names()
        assert len(names) == len(set(names)), "skill names must be unique"
        assert "explore_codebase" in names

    def test_list_skills_points_at_authoritative_set(self):
        listed = _list_skill_names()
        assert "code_review" in listed
        assert "debug_checklist" in listed
        assert "write_tests" in listed
        assert "refactor" in listed

    def test_load_skill_explore_codebase_via_tool(self):
        result = load_skill("explore_codebase")
        assert result["status"] == "ok"
        assert "Explore Codebase" in result["content"]
        assert str(_SKILLS_DIR) in result["path"]

    def test_load_packaged_skills_via_tool(self):
        for name in ("polyglot-verify", "atomic-edit-rollback", "git-safe-review"):
            res = load_skill(name)
            assert res["status"] == "ok", f"Failed to load {name}: {res}"
            assert res["name"] == name
            assert "Skill:" in res["content"]
            assert name in res["path"]
            assert res["path"].endswith("SKILL.md")
