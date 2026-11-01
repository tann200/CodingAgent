"""
Skill tool — load and return the content of a named skill from the skills directory.

Skills are reusable prompt templates or instruction sets stored as markdown files in
src/config/agent-brain/skills/.  The LLM can call load_skill("name") to retrieve a
skill's instructions at runtime without the skill being baked into every system prompt.

This mirrors opencode's skills/slash-command loading pattern.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from src.tools._tool import tool, PermissionKind

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_SKILLS_DIR = _CONFIG_DIR / "agent-brain" / "skills"
_SKILLS_DIRS = [
    _CONFIG_DIR / "skills",
    _SKILLS_DIR,
]


def _find_skill_file(name: str) -> Path | None:
    """Locate a skill file across configured search directories.

    Supports:
    - Package bundle directory: ``<dir>/<name>/SKILL.md``
    - Flat markdown file: ``<dir>/<name>.md``
    - Kebab-case and snake_case variants.
    """
    clean = name.strip()
    candidates = [clean, clean.replace("-", "_"), clean.replace("_", "-")]

    for sdir in _SKILLS_DIRS:
        if not sdir.exists():
            continue
        for cand in candidates:
            # 1. Directory bundle: <cand>/SKILL.md
            bundle_upper = sdir / cand / "SKILL.md"
            if bundle_upper.is_file():
                return bundle_upper
            bundle_lower = sdir / cand / "skill.md"
            if bundle_lower.is_file():
                return bundle_lower
            # 2. Flat file: <cand>.md
            flat_file = sdir / f"{cand}.md"
            if flat_file.is_file():
                return flat_file
    return None


@tool(tags=["coding", "planning", "debug", "review"], permission_kind=PermissionKind.READ_FILE)
def load_skill(name: str) -> Dict[str, Any]:
    """Load a named skill (prompt template) from the skills directory.

    Skills are markdown files in src/config/skills/ or src/config/agent-brain/skills/
    that provide reusable instructions, checklists, or workflow descriptions that the agent
    can inject into its context on demand.

    Args:
        name: Skill name (filename or directory name without .md extension). Use list_skills
              first to discover available skills.

    Returns:
        status, name, content (skill text), path.
    """
    if not name or not isinstance(name, str):
        return {"status": "error", "error": "name must be a non-empty string"}

    # Sanitise: reject path traversal
    clean = name.strip().lstrip("/\\")
    if ".." in clean or "/" in clean or "\\" in clean:
        return {
            "status": "error",
            "error": f"Invalid skill name: '{name}'. Must be a simple name, not a path.",
        }

    skill_path = _find_skill_file(clean)
    if not skill_path or not skill_path.exists():
        available = _list_skill_names()
        return {
            "status": "not_found",
            "error": f"Skill '{name}' not found.",
            "available": available,
        }

    try:
        content = skill_path.read_text(encoding="utf-8")
        return {
            "status": "ok",
            "name": clean,
            "content": content,
            "path": str(skill_path),
        }
    except Exception as exc:
        return {"status": "error", "error": f"Failed to read skill '{name}': {exc}"}


@tool(tags=["coding", "planning", "debug", "review"], permission_kind=PermissionKind.NONE)
def list_skills() -> Dict[str, Any]:
    """List all available skills in the skills directory.

    Returns:
        status, skills (list of skill names without .md extension).
    """
    names = _list_skill_names()
    return {"status": "ok", "skills": names}


def _list_skill_names() -> list[str]:
    names: set[str] = set()
    for sdir in _SKILLS_DIRS:
        if not sdir.exists():
            continue
        # Scan flat markdown files
        for p in sdir.glob("*.md"):
            if p.is_file():
                names.add(p.stem)
        # Scan directory bundles with SKILL.md or skill.md
        for p in sdir.iterdir():
            if p.is_dir() and ((p / "SKILL.md").is_file() or (p / "skill.md").is_file()):
                names.add(p.name)
    return sorted(names)
