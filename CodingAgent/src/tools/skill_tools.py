"""
Skill tool — load and return the content of a named skill from the skills directory.

Skills are reusable prompt templates or instruction sets stored as markdown files in
src/config/skills/.  The LLM can call load_skill("name") to retrieve a skill's
instructions at runtime without the skill being baked into every system prompt.

This mirrors opencode's skills/slash-command loading pattern.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from src.tools._tool import tool

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent.parent / "config" / "skills"


@tool(tags=["coding", "planning", "debug", "review"])
def load_skill(name: str) -> Dict[str, Any]:
    """Load a named skill (prompt template) from the skills directory.

    Skills are markdown files in src/config/skills/ that provide reusable
    instructions, checklists, or workflow descriptions that the agent can
    inject into its context on demand.

    Args:
        name: Skill name (filename without .md extension).  Use list_skills
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

    if not _SKILLS_DIR.exists():
        return {
            "status": "error",
            "error": f"Skills directory not found at {_SKILLS_DIR}. Create it and add .md skill files.",
        }

    skill_path = _SKILLS_DIR / f"{clean}.md"
    if not skill_path.exists():
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


@tool(tags=["coding", "planning", "debug", "review"])
def list_skills() -> Dict[str, Any]:
    """List all available skills in the skills directory.

    Returns:
        status, skills (list of skill names without .md extension).
    """
    names = _list_skill_names()
    return {"status": "ok", "skills": names}


def _list_skill_names() -> list:
    if not _SKILLS_DIR.exists():
        return []
    return sorted(p.stem for p in _SKILLS_DIR.glob("*.md") if p.is_file())
