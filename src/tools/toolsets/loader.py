"""Toolset loader — maps roles to YAML-defined tool lists.

YAML toolsets live in this directory (``src/tools/toolsets/``).  The loader
also falls back to ``src/config/toolsets/`` so existing deployments that
customised files there continue to work.

Adding a new toolset
--------------------
Create a ``my_role.yaml`` file in this directory::

    name: my_role
    description: Tools for my custom role
    tools:
      - read_file
      - bash
      - my_custom_tool

Then map the role in ``get_toolset_for_role()`` below, or call
``load_toolset("my_role")`` directly.

Adding a new role synonym
--------------------------
Extend the ``synonym_to_canonical`` dict in ``get_toolset_for_role()``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Search paths — tools-local directory first, legacy config directory second
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).parent          # src/tools/toolsets/
_CONFIG_DIR = Path(__file__).parent.parent.parent / "config" / "toolsets"  # src/config/toolsets/

_SEARCH_DIRS: List[Path] = [_TOOLS_DIR, _CONFIG_DIR]

_cache: Dict[str, Dict] = {}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _find_toolset_path(name: str) -> Optional[Path]:
    """Return the first YAML file found for *name* across all search dirs."""
    for d in _SEARCH_DIRS:
        candidate = d / f"{name}.yaml"
        if candidate.exists():
            return candidate
    return None


def load_toolset(name: str) -> Optional[Dict]:
    """Load and cache a toolset by name. Returns the raw YAML dict or None."""
    if name in _cache:
        return _cache[name]
    path = _find_toolset_path(name)
    if not path:
        return None
    if _yaml is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            toolset = _yaml.safe_load(f)
            _cache[name] = toolset
            return toolset
    except Exception:
        return None


def get_tools_for_toolset(name: str) -> List[str]:
    """Return the list of tool names for a given toolset, or [] if not found."""
    toolset = load_toolset(name)
    if toolset and "tools" in toolset:
        return list(toolset["tools"])
    return []


def list_available_toolsets() -> List[str]:
    """Return sorted names of all YAML toolset files found across search dirs."""
    names: set = set()
    for d in _SEARCH_DIRS:
        if d.exists():
            for p in d.glob("*.yaml"):
                names.add(p.stem)
    return sorted(names)


def get_toolset_description(name: str) -> str:
    """Return the description field from a toolset YAML, or empty string."""
    ts = load_toolset(name)
    return (ts or {}).get("description", "")


def invalidate_cache() -> None:
    """Clear the in-memory YAML cache (useful in tests or after hot-reload)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Role → toolset mapping
# ---------------------------------------------------------------------------

# Canonical role names and their toolset file names
_CANONICAL_TO_TOOLSET: Dict[str, str] = {
    "operational": "coding",
    "strategic":   "planning",
    "reviewer":    "review",
    "debugger":    "debug",
    "analyst":     "planning",
}

# Synonyms accepted in addition to canonical names
_SYNONYM_TO_CANONICAL: Dict[str, str] = {
    # operational
    "operational": "operational",
    "coder":       "operational",
    "developer":   "operational",
    "coding":      "operational",
    # strategic
    "strategic":   "strategic",
    "planner":     "strategic",
    "planning":    "strategic",
    "plan":        "strategic",
    # reviewer
    "review":      "reviewer",
    "reviewer":    "reviewer",
    "audit":       "reviewer",
    # debugger
    "debug":       "debugger",
    "debugger":    "debugger",
    # analyst
    "analysis":    "analyst",
    "analyst":     "analyst",
    "researcher":  "analyst",
    "research":    "analyst",
    "scout":       "analyst",
}


def get_toolset_for_role(role: str) -> str:
    """Map a role name (or synonym) to a toolset file name.

    Falls back to ``"coding"`` for unknown roles.
    """
    canonical = _SYNONYM_TO_CANONICAL.get((role or "").strip().lower(), "operational")
    return _CANONICAL_TO_TOOLSET.get(canonical, "coding")


def get_tools_for_role(role: str) -> List[str]:
    """Convenience: map role → toolset → tool list in one call."""
    return get_tools_for_toolset(get_toolset_for_role(role))


# ---------------------------------------------------------------------------
# ToolsetManager — stateful helper used by the Orchestrator
# ---------------------------------------------------------------------------

class ToolsetManager:
    """Thin stateful wrapper used by the Orchestrator to select toolsets."""

    def __init__(self, base_tools: Optional[List[str]] = None) -> None:
        self.base_tools: List[str] = base_tools or []
        self._current_toolset: Optional[str] = None

    def select_toolset(self, role: str) -> List[str]:
        """Return tool list for *role*; falls back to base_tools if empty."""
        toolset_name = get_toolset_for_role(role)
        tools = get_tools_for_toolset(toolset_name)
        if not tools:
            return self.base_tools
        self._current_toolset = toolset_name
        return tools

    def get_current_toolset(self) -> Optional[str]:
        return self._current_toolset

    def get_toolset_tools(self, name: str) -> List[str]:
        return get_tools_for_toolset(name)
