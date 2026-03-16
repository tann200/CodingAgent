from __future__ import annotations
import os
import yaml
from pathlib import Path
from typing import Dict, List, Optional


_TOOLSETS_DIR = Path(__file__).parent
_cache: Dict[str, Dict] = {}


def load_toolset(name: str) -> Optional[Dict]:
    """Load a toolset YAML file by name."""
    if name in _cache:
        return _cache[name]

    toolset_path = _TOOLSETS_DIR / f"{name}.yaml"
    if not toolset_path.exists():
        return None

    try:
        with open(toolset_path, "r") as f:
            toolset = yaml.safe_load(f)
            _cache[name] = toolset
            return toolset
    except Exception:
        return None


def get_tools_for_toolset(name: str) -> List[str]:
    """Get the list of tools for a given toolset."""
    toolset = load_toolset(name)
    if toolset and "tools" in toolset:
        return toolset["tools"]
    return []


def get_toolset_for_role(role: str) -> str:
    """Map a role to the appropriate toolset."""
    role_to_toolset = {
        "planner": "planning",
        "strategic": "planning",
        "coder": "coding",
        "operational": "coding",
        "execution": "coding",
        "reviewer": "review",
        "review": "review",
        "researcher": "planning",
        "debugger": "debug",
    }
    return role_to_toolset.get(role, "coding")


def list_available_toolsets() -> List[str]:
    """List all available toolset names."""
    toolsets = []
    for f in _TOOLSETS_DIR.glob("*.yaml"):
        toolsets.append(f.stem)
    return toolsets


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
