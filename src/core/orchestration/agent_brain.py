"""AgentBrainManager: In-memory caching for agent-brain configuration.

This module provides a singleton AgentBrainManager that loads and caches
identity, roles, and skills from src/config/agent-brain/ for fast access.
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict, Optional
import re
import logging
import threading

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
    # MED-14 fix: lock guards the singleton creation so concurrent threads don't
    # each call super().__new__() and end up with multiple instances.
    _instance_lock: threading.Lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
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
        # SK-1: Cache parsed frontmatter per skill for summary listing
        self._skill_meta_cache: Dict[str, dict] = {}
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
                # SK-1: Cache frontmatter for summary listing
                fm = _parse_front_matter(content)
                if fm is not None:
                    self._skill_meta_cache[skill_name] = fm
                else:
                    # Derive minimal metadata from filename / first heading for
                    # skills that pre-date the frontmatter convention.
                    self._skill_meta_cache[skill_name] = {"name": skill_name}
                logger.info(f"Loaded skill: {skill_name}")

        # RS-1: Load remote skills from configured URLs (additive, local wins)
        try:
            from src.core.orchestration.remote_skills import load_all_remote_skills

            remote = load_all_remote_skills()
            for skill_name, content in remote.items():
                if skill_name in self._skill_cache:
                    logger.debug(
                        "remote_skills: skipping %s — shadowed by local skill",
                        skill_name,
                    )
                    continue
                self._skill_cache[skill_name] = _extract_body(content)
                fm = _parse_front_matter(content)
                if fm is not None:
                    self._skill_meta_cache[skill_name] = fm
                else:
                    self._skill_meta_cache[skill_name] = {"name": skill_name}
                logger.info("remote_skills: loaded remote skill %s", skill_name)
        except Exception as exc:
            logger.warning("remote_skills: failed to load remote skills: %s", exc)

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

    def list_skills_summary(self) -> str:
        """SK-1: Return a compact multi-line summary of available skills.

        Each line has the form:
            • <name> — <triggers-or-description>

        This is injected verbatim into the system prompt so the agent always
        knows which skills exist and when to apply them without needing to call
        ``list_skills`` first.
        """
        lines = []
        for skill_name in sorted(self._skill_meta_cache):
            meta = self._skill_meta_cache[skill_name]
            display_name = meta.get("name", skill_name)
            # Prefer triggers list as a compact "when to use" hint
            triggers = meta.get("triggers", None)
            if triggers:
                if isinstance(triggers, list):
                    hint = ", ".join(str(t) for t in triggers[:5])
                else:
                    hint = str(triggers)
            else:
                hint = meta.get("description", "general coding skill")
            lines.append(f"  • {display_name} — {hint}")
        if not lines:
            return ""
        return "\n".join(lines)

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

        return {"content": role, "p2p_topic": topics.get(role_name, "")}

    def compile_system_prompt(self, role_name: str = "operational") -> str:
        """Compile a full system prompt with role, SOUL, LAWS, and available skills."""
        role_content = self.get_role(role_name)
        if not role_content:
            role_content = "You are a helpful coding assistant."

        parts = []

        parts.append("<system_role>")
        parts.append(role_content)
        parts.append("</system_role>")

        soul = self.get_identity("soul")
        # P1-C fix: SOUL.md (operating principles) is injected here only as a comment
        # placeholder.  The actual injection is handled exclusively by ContextBuilder
        # (context_builder.py build_prompt() line ~492) which wraps it in <identity>
        # tags.  Injecting it again here under <operating_principles> caused the full
        # SOUL.md content to appear twice in every LLM call, doubling the token cost
        # for that section.  Remove the injection; keep the variable for potential
        # future use (e.g. display in UI) but do not append it to `parts`.
        _ = soul  # referenced to avoid unused-variable lint warnings

        laws = self.get_identity("laws")
        if laws:
            parts.append("\n<core_laws>")
            parts.append(laws)
            parts.append("</core_laws>")

        # SK-1: Inject available skills so the agent doesn't need to call
        # list_skills first — mirrors opencode's proactive skills listing.
        skills_summary = self.list_skills_summary()
        if skills_summary:
            parts.append("\n<available_skills>")
            parts.append(
                "The following skills are available. Load one with the `load_skill` tool "
                "when the task matches its triggers."
            )
            parts.append(skills_summary)
            parts.append("</available_skills>")

        return "\n".join(parts)

    def reload(self):
        """Reload all caches from disk."""
        self._identity_cache.clear()
        self._role_cache.clear()
        self._skill_cache.clear()
        self._skill_meta_cache.clear()
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
    """Compile the final system prompt by injecting SOUL, LAWS, and available skills."""
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

    # SK-1: Inject available skills (mirrors compile_system_prompt() on the manager)
    skills_summary = manager.list_skills_summary()
    if skills_summary:
        parts.append("\n<available_skills>")
        parts.append(
            "The following skills are available. Load one with the `load_skill` tool "
            "when the task matches its triggers."
        )
        parts.append(skills_summary)
        parts.append("</available_skills>")

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
