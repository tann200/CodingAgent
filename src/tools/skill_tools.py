"""Skill tools — progressive-disclosure discovery and on-demand loading (TASK-32).

Backed by ``discover_skills(workdir)`` across:
  1. ``<workspace>/.codingagent/skills/``
  2. ``<workspace>/.agents/skills/``
  3. ``<workspace>/.claude/skills/``
  4. ``~/.codingagent/skills/``
  5. Bundled ``src/config/skills/`` and ``src/config/agent-brain/skills/``

Project-scoped skills are ``safe_resolve``-confined and wrapped in a
per-process nonce-fenced ``<project_skill>`` untrusted envelope.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.context.agent_brain_loading import (
    discover_skills,
    find_skill_meta,
    read_skill_content,
)
from src.tools._tool import PermissionKind, tool

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_SKILLS_DIR = _CONFIG_DIR / "agent-brain" / "skills"
_SKILLS_DIRS = [
    _CONFIG_DIR / "skills",
    _SKILLS_DIR,
]


def _resolve_active_workdir(workdir: Optional[Path] = None) -> Optional[Path]:
    if workdir is not None:
        return Path(workdir)
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        orch = _PARENT_ORCHESTRATOR_VAR.get(None)
        if orch is not None and getattr(orch, "working_dir", None):
            return Path(orch.working_dir)
    except Exception:
        pass
    return None


def _find_skill_file(name: str, workdir: Optional[Path] = None) -> Path | None:
    """Locate a skill file across unified discovery scopes (compat helper)."""
    catalog = discover_skills(_resolve_active_workdir(workdir))
    meta = find_skill_meta(name, catalog)
    return meta.path if meta is not None else None


def _list_skill_names(workdir: Optional[Path] = None) -> list[str]:
    catalog = discover_skills(_resolve_active_workdir(workdir))
    return sorted(catalog.keys())


@tool(tags=["coding", "planning", "debug", "review"], permission_kind=PermissionKind.READ_FILE)
def load_skill(name: str, workdir: Path | None = None) -> Dict[str, Any]:
    """Load a named skill playbook on demand across workspace, user, and bundled scopes.

    Args:
        name: Skill name (e.g. ``debugging``, ``polyglot-verify``). Use ``list_skills``
              or the static skill catalog to see available skill names.

    Returns:
        status, name, description, provenance, content, path.
    """
    if not name or not isinstance(name, str):
        return {"status": "error", "error": "name must be a non-empty string"}

    clean = name.strip().lstrip("/\\")
    if ".." in clean or "/" in clean or "\\" in clean:
        return {
            "status": "error",
            "error": f"Invalid skill name: '{name}'. Must be a simple name, not a path.",
        }

    catalog = discover_skills(_resolve_active_workdir(workdir))
    meta = find_skill_meta(clean, catalog)
    if meta is None or not meta.path.exists():
        return {
            "status": "not_found",
            "error": f"Skill '{name}' not found.",
            "available": sorted(catalog.keys()),
        }

    try:
        content = read_skill_content(meta)
        return {
            "status": "ok",
            "name": meta.name,
            "description": meta.description,
            "provenance": meta.provenance,
            "source_scope": meta.source_scope,
            "content": content,
            "path": str(meta.path),
        }
    except Exception as exc:
        return {"status": "error", "error": f"Failed to read skill '{name}': {exc}"}


@tool(tags=["coding", "planning", "debug", "review"], permission_kind=PermissionKind.NONE)
def list_skills(workdir: Path | None = None) -> Dict[str, Any]:
    """List all available skills across workspace, user, and bundled scopes."""
    catalog = discover_skills(_resolve_active_workdir(workdir))
    return {
        "status": "ok",
        "skills": sorted(catalog.keys()),
        "catalog": [
            {
                "name": m.name,
                "description": m.description,
                "provenance": m.provenance,
                "source_scope": m.source_scope,
            }
            for m in catalog.values()
        ],
    }
