"""AgentBrainManager: In-memory caching for agent-brain configuration.

This module provides a singleton AgentBrainManager that loads and caches
identity, roles, and skills from src/config/agent-brain/ for fast access.
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict, Optional
import re
import logging

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).parents[3]


def _agent_brain_dir() -> Path:
    return _repo_root() / "src" / "config" / "agent-brain"


def _parse_front_matter(text: str) -> Optional[dict]:
    """Parse YAML front-matter and return a dict."""
    if not text or not text.startswith("---"):
        return None
    m = re.match(r"^---\s*\n(.*?)(\n---\s*\n)", text, flags=re.S)
    if not m:
        return None
    body = m.group(1)
    try:
        import yaml

        data = yaml.safe_load(body)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    out = {}
    for line in body.splitlines():
        if not line.strip() or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def _extract_body(text: str) -> str:
    """Extract body text after front-matter."""
    fm = _parse_front_matter(text)
    if fm is not None:
        return re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.S).strip()
    return text.strip()


class AgentBrainManager:
    """Singleton manager for agent-brain configuration with in-memory caching."""

    _instance: Optional["AgentBrainManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._identity_cache: Dict[str, str] = {}
        self._role_cache: Dict[str, str] = {}
        self._skill_cache: Dict[str, str] = {}
        self._load_all()
        self._initialized = True
        logger.info("AgentBrainManager initialized with caches")

    def _load_all(self):
        """Load all identity, role, and skill files into memory."""
        brain_dir = _agent_brain_dir()

        # Load identity files
        identity_dir = brain_dir / "identity"
        if identity_dir.exists():
            for fname in ["SOUL.md", "LAWS.md"]:
                fpath = identity_dir / fname
                if fpath.exists():
                    content = fpath.read_text(encoding="utf-8")
                    key = fname.replace(".md", "").lower()
                    self._identity_cache[key] = _extract_body(content)
                    logger.info(f"Loaded identity: {key}")

        # Load roles
        roles_dir = brain_dir / "roles"
        if roles_dir.exists():
            for fpath in roles_dir.glob("*.md"):
                role_name = fpath.stem
                content = fpath.read_text(encoding="utf-8")
                self._role_cache[role_name] = _extract_body(content)
                logger.info(f"Loaded role: {role_name}")

        # Load skills
        skills_dir = brain_dir / "skills"
        if skills_dir.exists():
            for fpath in skills_dir.glob("*.md"):
                skill_name = fpath.stem
                content = fpath.read_text(encoding="utf-8")
                self._skill_cache[skill_name] = _extract_body(content)
                logger.info(f"Loaded skill: {skill_name}")

        logger.info(
            f"AgentBrainManager loaded: {len(self._identity_cache)} identities, "
            f"{len(self._role_cache)} roles, {len(self._skill_cache)} skills"
        )

    def get_identity(self, name: str = "soul") -> str:
        """Get identity content by name (soul, laws)."""
        key = name.lower()
        return self._identity_cache.get(key, "")

    def get_role(self, role_name: str) -> str:
        """Get role content by name (strategic, operational, etc.)."""
        return self._role_cache.get(role_name, "")

    def get_skill(self, skill_name: str) -> str:
        """Get skill content by name (dry, context_hygiene, etc.)."""
        return self._skill_cache.get(skill_name, "")

    def get_all_roles(self) -> Dict[str, str]:
        """Get all cached roles."""
        return self._role_cache.copy()

    def get_all_skills(self) -> Dict[str, str]:
        """Get all cached skills."""
        return self._skill_cache.copy()

    def get_role_with_topics(self, role_name: str) -> Dict[str, str]:
        """Get role content and P2P topic for the role."""
        role = self.get_role(role_name)
        if not role:
            return {}

        topics = {
            "scout": "agent.scout.broadcast",
            "researcher": "agent.researcher.broadcast",
            "reviewer": "agent.reviewer.broadcast",
            "tester": "agent.tester.broadcast",
        }

        return {"content": role, "p2p_topic": topics.get(role_name)}

    def compile_system_prompt(self, role_name: str = "operational") -> str:
        """Compile a full system prompt with role, SOUL, and LAWS."""
        role_content = self.get_role(role_name)
        if not role_content:
            role_content = "You are a helpful coding assistant."

        parts = []

        parts.append("<system_role>")
        parts.append(role_content)
        parts.append("</system_role>")

        soul = self.get_identity("soul")
        if soul:
            parts.append("\n<operating_principles>")
            parts.append(soul)
            parts.append("</operating_principles>")

        laws = self.get_identity("laws")
        if laws:
            parts.append("\n<core_laws>")
            parts.append(laws)
            parts.append("</core_laws>")

        return "\n".join(parts)

    def reload(self):
        """Reload all caches from disk."""
        self._identity_cache.clear()
        self._role_cache.clear()
        self._skill_cache.clear()
        self._load_all()
        logger.info("AgentBrainManager reloaded")


def get_agent_brain_manager() -> AgentBrainManager:
    """Get the singleton AgentBrainManager instance."""
    return AgentBrainManager()


# Backward compatibility: keep old function signatures working
def _repo_root_old() -> Path:
    return Path(__file__).parents[3]


def _agent_brain_dir_old() -> Path:
    return _repo_root_old() / "agent-brain"


def _load_core_component(filename: str) -> str:
    """Load a core markdown component from the agent-brain directory."""
    manager = get_agent_brain_manager()
    key = filename.replace(".md", "").lower()
    return manager.get_identity(key)


def _compile_system_prompt(role_content: str) -> str:
    """Compile the final system prompt by injecting SOUL and LAWS."""
    if not role_content:
        return ""

    manager = get_agent_brain_manager()

    parts = []
    parts.append("<system_role>")
    parts.append(role_content)
    parts.append("</system_role>")

    soul = manager.get_identity("soul")
    if soul:
        parts.append("\n<operating_principles>")
        parts.append(soul)
        parts.append("</operating_principles>")

    laws = manager.get_identity("laws")
    if laws:
        parts.append("\n<core_laws>")
        parts.append(laws)
        parts.append("</core_laws>")

    return "\n".join(parts)


def load_system_prompt(
    name: Optional[str] = None, path: Optional[Path] = None
) -> Optional[str]:
    """Load a system prompt by name or explicit path.

    Now uses AgentBrainManager for caching.
    """
    manager = get_agent_brain_manager()

    try:
        if path:
            p = Path(path)
            if p.exists() and p.is_file():
                txt = p.read_text(encoding="utf-8")
                fm = _parse_front_matter(txt)
                if fm is not None:
                    rest = re.sub(r"^---\s*\n.*?\n---\s*\n", "", txt, flags=re.S)
                    return _compile_system_prompt(rest.strip())
                return _compile_system_prompt(txt)

        if name:
            role = manager.get_role(name)
            if role:
                return _compile_system_prompt(role)

        return manager.compile_system_prompt("operational")

    except Exception:
        return None
