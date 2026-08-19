"""Helpers for loading identity, role, and skill prompts from disk."""

from __future__ import annotations

from pathlib import Path
from typing import Callable


def load_prompt_directory(directory: Path, read_text_cached: Callable[[Path], str | None]) -> dict[str, str]:
    """Load all `*.md` files in a directory into a stem->content map."""
    loaded: dict[str, str] = {}
    if not directory.exists():
        return loaded
    for file_path in directory.glob("*.md"):
        loaded[file_path.stem] = read_text_cached(file_path) or ""
    return loaded


def merge_workspace_skill_overrides(
    base_skills: dict[str, str],
    workspace_skill_dirs: list[Path],
    read_text_cached: Callable[[Path], str | None],
) -> dict[str, str]:
    """Overlay workspace skills on top of built-in skills."""
    merged = dict(base_skills)
    for skill_dir in workspace_skill_dirs:
        if not skill_dir.exists():
            continue
        for skill_file in skill_dir.glob("*.md"):
            content = read_text_cached(skill_file)
            if content:
                merged[skill_file.stem] = content
    return merged
