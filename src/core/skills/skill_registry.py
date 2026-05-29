"""
skill_registry.py — Registry for managing available skills.
"""

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from .skill_loader import Skill, SkillLoader

logger = logging.getLogger(__name__)


@dataclass
class SkillRegistry:
    """Registry for managing skills."""

    loader: SkillLoader
    _skill_aliases: Dict[str, str] = field(default_factory=dict)
    _skill_tags: Dict[str, List[str]] = field(default_factory=dict)
    # E1: Lock guards _skill_aliases and _skill_tags against concurrent
    # read/write from the scheduler thread and registration API callers.
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __init__(self, skills_dir: Optional[Path] = None):
        """Initialize registry."""
        object.__setattr__(self, "loader", SkillLoader(skills_dir))
        object.__setattr__(self, "_skill_aliases", {})
        object.__setattr__(self, "_skill_tags", {})
        object.__setattr__(self, "_lock", threading.Lock())
        self.reload()

    def reload(self) -> None:
        """Reload all skills from the loader."""
        self.loader.load_skills(reload=True)
        with self._lock:
            self._skill_aliases.clear()
            self._skill_tags.clear()
        logger.info(f"Reloaded skill registry with {len(self.list_skills())} skills")

    def register_skill_alias(self, alias: str, skill_name: str) -> None:
        """
        Register an alias for a skill.

        Args:
            alias: Alias name
            skill_name: Actual skill name
        """
        if skill_name not in self.list_skills():
            logger.warning(
                f"Cannot register alias '{alias}' for unknown skill '{skill_name}'"
            )
            return

        with self._lock:
            self._skill_aliases[alias] = skill_name
        logger.debug(f"Registered alias '{alias}' -> '{skill_name}'")

    def register_skill_tags(self, skill_name: str, tags: List[str]) -> None:
        """
        Register tags for a skill.

        Args:
            skill_name: Skill name
            tags: List of tags
        """
        if skill_name not in self.list_skills():
            logger.warning(f"Cannot register tags for unknown skill '{skill_name}'")
            return

        with self._lock:
            self._skill_tags[skill_name] = tags
        logger.debug(f"Registered tags {tags} for skill '{skill_name}'")

    def get_skill(self, name: str) -> Optional[Skill]:
        """
        Get a skill by name or alias.

        Args:
            name: Skill name or alias

        Returns:
            Skill object or None if not found
        """
        # Check if it's an alias first
        with self._lock:
            actual_name = self._skill_aliases.get(name, name)

        # If it's still not found and looks like it might be an alias, try without prefix
        if actual_name not in self.loader.list_skills() and "." in actual_name:
            # Try the part after the last dot as a fallback
            parts = actual_name.split(".")
            if parts[-1] in self.loader.list_skills():
                actual_name = parts[-1]

        skill = self.loader.get_skill(actual_name)
        if skill:
            logger.debug(f"Retrieved skill '{actual_name}' (requested: '{name}')")
        else:
            logger.debug(f"Skill not found: '{name}' (checked as '{actual_name}')")
        return skill

    def list_skills(self) -> List[str]:
        """
        List all available skill names.

        Returns:
            List of skill names
        """
        return self.loader.list_skills()

    def list_skill_aliases(self) -> Dict[str, str]:
        """
        List all skill aliases.

        Returns:
            Dictionary mapping aliases to skill names
        """
        with self._lock:
            return self._skill_aliases.copy()

    def get_skill_tags(self, skill_name: str) -> List[str]:
        """
        Get tags for a skill.

        Args:
            skill_name: Skill name

        Returns:
            List of tags (empty list if no tags)
        """
        with self._lock:
            return self._skill_tags.get(skill_name, [])

    def find_skills_by_tag(self, tag: str) -> List[str]:
        """
        Find skills that have a specific tag.

        Args:
            tag: Tag to search for

        Returns:
            List of skill names that have the tag
        """
        matching_skills = []
        with self._lock:
            for skill_name, tags in self._skill_tags.items():
                if tag in tags:
                    matching_skills.append(skill_name)
        return matching_skills

    def get_skill_content(self, name: str) -> Optional[str]:
        """
        Get the content of a skill by name or alias.

        Args:
            name: Skill name or alias

        Returns:
            Skill content or None if not found
        """
        skill = self.get_skill(name)
        return skill.content if skill else None


# Global skill registry instance
_skill_registry: Optional[SkillRegistry] = None
# E1: Lock guards _skill_registry lazy-init so concurrent threads don't
# create separate instances.
_SKILL_REGISTRY_LOCK = threading.Lock()


def get_skill_registry() -> SkillRegistry:
    """Get the global skill registry instance."""
    global _skill_registry
    if _skill_registry is None:
        with _SKILL_REGISTRY_LOCK:
            if _skill_registry is None:
                _skill_registry = SkillRegistry()
    return _skill_registry


def reload_skill_registry() -> None:
    """Reload the global skill registry."""
    registry = get_skill_registry()
    registry.reload()


if __name__ == "__main__":
    # Simple test
    registry = SkillRegistry()
    skills = registry.list_skills()
    print(f"Loaded {len(skills)} skills:")
    for name in skills:
        skill = registry.get_skill(name)
        if skill:
            print(f"  - {name}: {skill.description[:50]}...")
        else:
            print(f"  - {name}: SKILL NOT FOUND")
