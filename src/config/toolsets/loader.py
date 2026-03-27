from __future__ import annotations
import yaml
from pathlib import Path
from typing import Dict, List, Optional


# VOL7-5: Use Path(__file__).parent so YAML files are found regardless of the
# current working directory (fixes test environments and CI that run from a
# different root than the repository root).
_DIR = Path(__file__).parent
_cache: Dict[str, Dict] = {}


def _find_toolset_path(name: str) -> Optional[Path]:
    candidate = _DIR / f"{name}.yaml"
    if candidate.exists():
        return candidate
    return None


def load_toolset(name: str) -> Optional[Dict]:
    """Load a toolset YAML file by name."""
    if name in _cache:
        return _cache[name]
    path = _find_toolset_path(name)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            toolset = yaml.safe_load(f)
            _cache[name] = toolset
            return toolset
    except Exception:
        return None


def get_tools_for_toolset(name: str) -> List[str]:
    """Get the list of tools for a given toolset."""
    toolset = load_toolset(name)
    if toolset and "tools" in toolset:
        return list(toolset["tools"])
    return []


def get_toolset_for_role(role: str) -> str:
    """Map an input role name to a toolset name.

    Canonical role names:
      - analyst
      - strategic
      - operational
      - reviewer
      - debugger

    This function accepts common synonyms and maps them to a canonical role,
    then returns the toolset name associated with that canonical role.
    """
    role_in = (role or "").strip().lower()

    # map input synonyms -> canonical role name
    synonym_to_canonical = {
        # operational (execution / coder)
        "operational": "operational",
        "coder": "operational",
        "developer": "operational",
        "coding": "operational",
        # strategic / planner
        "strategic": "strategic",
        "planner": "strategic",
        "planning": "strategic",
        "plan": "strategic",
        # reviewer
        "review": "reviewer",
        "reviewer": "reviewer",
        "audit": "reviewer",
        # debugger
        "debug": "debugger",
        "debugger": "debugger",
        # analyst
        "analysis": "analyst",
        "analyst": "analyst",
    }

    canonical = synonym_to_canonical.get(role_in, "operational")

    # map canonical role -> toolset name
    canonical_to_toolset = {
        "operational": "coding",
        "strategic": "planning",
        "reviewer": "review",
        "debugger": "debug",
        "analyst": "planning",
    }

    return canonical_to_toolset.get(canonical, "coding")


def list_available_toolsets() -> List[str]:
    """List all available toolset names."""
    names = set()
    if _DIR.exists():
        for p in _DIR.glob("*.yaml"):
            names.add(p.stem)
    return sorted(names)


def get_toolset_description(name: str) -> str:
    """Get the description of a toolset."""
    toolset = load_toolset(name)
    if toolset:
        return toolset.get("description", "")
    return ""


class ToolsetManager:
    def __init__(self, base_tools: List[str] = None):
        self.base_tools = base_tools or []
        self._current_toolset: Optional[str] = None

    def select_toolset(self, role: str) -> List[str]:
        """Select the appropriate toolset based on role."""
        toolset_name = get_toolset_for_role(role)
        toolset_tools = get_tools_for_toolset(toolset_name)

        if not toolset_tools:
            return self.base_tools

        self._current_toolset = toolset_name
        return toolset_tools

    def get_current_toolset(self) -> Optional[str]:
        return self._current_toolset

    def get_toolset_tools(self, name: str) -> List[str]:
        return get_tools_for_toolset(name)
