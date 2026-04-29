"""
skill_loader.py — Load SKILL.md files for agent capabilities.
"""

import yaml
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """Represents a loaded skill from SKILL.md."""

    name: str
    description: str
    content: str
    file_path: Path
    metadata: Dict[str, Any]

    def __post_init__(self):
        """Ensure metadata is a dict."""
        if not isinstance(self.metadata, dict):
            self.metadata = {}


class SkillLoader:
    """Loads and manages SKILL.md files."""

    def __init__(self, skills_dir: Optional[Path] = None):
        """
        Initialize skill loader.

        Args:
            skills_dir: Directory containing skill files. Defaults to ./skills
        """
        self.skills_dir = Path(skills_dir) if skills_dir else Path("./skills")
        self._skills: Dict[str, Skill] = {}
        self._loaded = False

    def load_skills(self, reload: bool = False) -> Dict[str, Skill]:
        """
        Load all skills from the skills directory.

        Args:
            reload: If True, reload skills even if already loaded

        Returns:
            Dictionary mapping skill names to Skill objects
        """
        if self._loaded and not reload:
            return self._skills

        self._skills.clear()

        if not self.skills_dir.exists():
            logger.warning(f"Skills directory does not exist: {self.skills_dir}")
            return self._skills

        # Look for SKILL.md files
        skill_files = list(self.skills_dir.rglob("SKILL.md"))
        logger.info(f"Found {len(skill_files)} SKILL.md files")

        for skill_file in skill_files:
            try:
                skill = self._load_skill_file(skill_file)
                if skill:
                    self._skills[skill.name] = skill
                    logger.debug(f"Loaded skill: {skill.name} from {skill_file}")
            except Exception as e:
                logger.error(f"Failed to load skill from {skill_file}: {e}")

        self._loaded = True
        logger.info(f"Loaded {len(self._skills)} skills")
        return self._skills

    def _load_skill_file(self, file_path: Path) -> Optional[Skill]:
        """
        Load a single SKILL.md file.

        Expected format:
        ---
        name: skill-name
        description: "Skill description"
        version: "1.0.0"
        ---
        # Skill content here
        ...
        """
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Could not read skill file {file_path}: {e}")
            return None

        # Parse frontmatter (YAML between --- lines)
        metadata = {}
        skill_content = content

        if content.startswith("---\n"):
            try:
                # Find end of frontmatter
                end_idx = content.find("\n---\n", 4)
                if end_idx != -1:
                    frontmatter = content[4:end_idx]
                    skill_content = content[end_idx + 5 :]  # Skip past "\n---\n"
                    metadata = yaml.safe_load(frontmatter) or {}
                else:
                    # No closing ---, treat as all content
                    skill_content = content[4:]  # Skip opening ---
                    metadata = {}
            except Exception as e:
                logger.warning(f"Could not parse frontmatter in {file_path}: {e}")
                metadata = {}
                skill_content = content

        # Extract name and description
        name = metadata.get("name") or file_path.stem
        description = metadata.get("description") or ""

        # Ensure we have a name
        if not name or name.strip() == "":
            logger.warning(f"Skill file {file_path} has no name, skipping")
            return None

        return Skill(
            name=name.strip(),
            description=description.strip(),
            content=skill_content.strip(),
            file_path=file_path,
            metadata=metadata,
        )

    def get_skill(self, name: str) -> Optional[Skill]:
        """
        Get a skill by name.

        Args:
            name: Skill name

        Returns:
            Skill object or None if not found
        """
        if not self._loaded:
            self.load_skills()
        return self._skills.get(name)

    def list_skills(self) -> List[str]:
        """
        List all available skill names.

        Returns:
            List of skill names
        """
        if not self._loaded:
            self.load_skills()
        return list(self._skills.keys())

    def get_skill_content(self, name: str) -> Optional[str]:
        """
        Get the content of a skill.

        Args:
            name: Skill name

        Returns:
            Skill content or None if not found
        """
        skill = self.get_skill(name)
        return skill.content if skill else None

    def reload(self) -> Dict[str, Skill]:
        """
        Reload all skills.

        Returns:
            Dictionary mapping skill names to Skill objects
        """
        return self.load_skills(reload=True)


# Global skill loader instance
_skill_loader: Optional[SkillLoader] = None


def get_skill_loader() -> SkillLoader:
    """Get the global skill loader instance."""
    global _skill_loader
    if _skill_loader is None:
        _skill_loader = SkillLoader()
    return _skill_loader


def load_skills(skills_dir: Optional[Path] = None) -> Dict[str, Skill]:
    """
    Load skills from the specified directory.

    Args:
        skills_dir: Directory containing skill files

    Returns:
        Dictionary mapping skill names to Skill objects
    """
    loader = SkillLoader(skills_dir)
    return loader.load_skills()


def get_skill(name: str, skills_dir: Optional[Path] = None) -> Optional[Skill]:
    """
    Get a skill by name.

    Args:
        name: Skill name
        skills_dir: Directory containing skill files

    Returns:
        Skill object or None if not found
    """
    if skills_dir:
        loader = SkillLoader(skills_dir)
    else:
        loader = get_skill_loader()
    return loader.get_skill(name)


if __name__ == "__main__":
    # Simple test
    loader = SkillLoader()
    skills = loader.load_skills()
    print(f"Loaded {len(skills)} skills:")
    for name, skill in skills.items():
        print(f"  - {name}: {skill.description[:50]}...")
