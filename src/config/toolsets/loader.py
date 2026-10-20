from __future__ import annotations
import json
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# VOL7-5: Use Path(__file__).parent so YAML files are found regardless of the
# current working directory (fixes test environments and CI that run from a
# different root than the repository root).
_DIR = Path(__file__).parent
_cache: Dict[str, Dict] = {}
# Format-aware cache keyed by (toolset_name, format, dir_path_str) to avoid
# collisions when the toolsets directory changes (tests monkeypatch _DIR).
_format_cache: Dict[Tuple[str, str, str], Dict] = {}


def _find_toolset_path(name: str) -> Optional[Path]:
    """Find a toolset file by name, preferring YAML but accepting JSON.

    Returns the Path to the first existing file matching <name>.yaml or
    <name>.json (YAML preferred)."""
    for ext in ("yaml", "yml", "json"):
        candidate = _DIR / f"{name}.{ext}"
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
        # Choose loader based on file suffix
        with open(path, "r", encoding="utf-8") as f:
            suffix = path.suffix.lower()
            if suffix in (".yaml", ".yml"):
                toolset = yaml.safe_load(f)
                fmt = "yaml"
            else:
                # .json
                toolset = json.load(f)
                fmt = "json"

            # Populate both the name-keyed cache (for backwards compatibility)
            # and the format-aware cache so subsequent model-aware lookups are
            # consistent.
            _cache[name] = toolset
            dir_key = str(_DIR)
            _format_cache[(name, fmt, dir_key)] = toolset
            return toolset
    except Exception:
        return None


def get_tools_for_toolset(name: str) -> List[str]:
    """Get the list of tools for a given toolset."""
    toolset = load_toolset(name)
    if toolset and "tools" in toolset:
        return list(toolset["tools"])
    return []


def resolve_toolset(
    name: str,
    registry_or_tools: Any,
    fail_closed: bool = True,
) -> List[str]:
    """Resolve a toolset against registered tool names.

    Parameters
    ----------
    name:
        Toolset name (e.g. 'coding', 'debug').
    registry_or_tools:
        A ToolRegistry instance, orchestrator, dict of tools, or collection of tool names.
    fail_closed:
        When True (default), raise KeyError if the toolset does not exist, has
        no tools, or references tools not present in registry_or_tools.

    Returns
    -------
    List[str]
        List of resolved tool names.
    """
    configured = get_tools_for_toolset(name)
    if not configured:
        if fail_closed:
            raise KeyError(f"Toolset {name!r} not found or contains no tools")
        return []

    if hasattr(registry_or_tools, "list") and callable(registry_or_tools.list):
        available = set(registry_or_tools.list())
    elif hasattr(registry_or_tools, "tools") and isinstance(registry_or_tools.tools, dict):
        available = set(registry_or_tools.tools.keys())
    elif isinstance(registry_or_tools, (set, frozenset, list, tuple)):
        available = set(registry_or_tools)
    elif isinstance(registry_or_tools, dict):
        available = set(registry_or_tools.keys())
    else:
        available = set()

    missing = [t for t in configured if t not in available]
    if missing and fail_closed:
        raise KeyError(
            f"Toolset {name!r} references configured tools absent from registry: {sorted(missing)}"
        )
    return [t for t in configured if t in available]


def validate_all_toolsets(
    registry_or_tools: Any,
    fail_closed: bool = True,
) -> Dict[str, List[str]]:
    """Validate all available toolsets against available tools.

    Returns a dict mapping toolset name to list of missing tool names.
    When fail_closed=True and any toolset has missing tools, raises KeyError.
    """
    errors: Dict[str, List[str]] = {}
    for ts_name in list_available_toolsets():
        configured = get_tools_for_toolset(ts_name)
        if hasattr(registry_or_tools, "list") and callable(registry_or_tools.list):
            available = set(registry_or_tools.list())
        elif hasattr(registry_or_tools, "tools") and isinstance(registry_or_tools.tools, dict):
            available = set(registry_or_tools.tools.keys())
        elif isinstance(registry_or_tools, (set, frozenset, list, tuple)):
            available = set(registry_or_tools)
        elif isinstance(registry_or_tools, dict):
            available = set(registry_or_tools.keys())
        else:
            available = set()
        missing = [t for t in configured if t not in available]
        if missing:
            errors[ts_name] = sorted(missing)

    if errors and fail_closed:
        details = "; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))
        raise KeyError(f"Toolsets contain unregistered tools: {details}")

    return errors


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
        "analyst": "analysis",
    }

    return canonical_to_toolset.get(canonical, "coding")


def get_tools_for_role(role: str) -> List[str]:
    """Return the tool names for the toolset selected for *role*."""
    return get_tools_for_toolset(get_toolset_for_role(role))


def clear_cache() -> None:
    """Invalidate the in-process toolset cache (useful after YAML edits)."""
    _cache.clear()
    _format_cache.clear()


def list_available_toolsets() -> List[str]:
    """List all available toolset names."""
    names = set()
    if _DIR.exists():
        for p in _DIR.glob("*.yaml"):
            names.add(p.stem)
        for p in _DIR.glob("*.yml"):
            names.add(p.stem)
        for p in _DIR.glob("*.json"):
            names.add(p.stem)
    return sorted(names)


def _is_small_model(model: Optional[str]) -> bool:
    """Heuristic: return True when *model* refers to a smaller/lighter model.

    This is intentionally simple and conservative: tokens like 'mini', 'small',
    'lite', '3.5', or well-known smaller families imply a small model. When
    model is None we default to True (yaml) to preserve existing behaviour.
    """
    if not model:
        return True
    m = model.lower()
    # Explicit big-model markers — if present, treat as big regardless of other tokens
    big_tokens = ("gpt-4", "gpt4", "4o", "gpt-4o")
    for t in big_tokens:
        if t in m:
            return False
    # Small-model markers
    small_tokens = (
        "mini",
        "small",
        "lite",
        "3.5",
        "gpt-3",
        "gpt3",
        "ada",
        "babbage",
        "curie",
        "turbo",
    )
    for t in small_tokens:
        if t in m:
            return True
    # Default to True for safety
    return True


def load_toolset_for_model(name: str, model: Optional[str]) -> Optional[Dict]:
    """Load a toolset preferring YAML for smaller models and JSON for bigger ones.

    This helper does not populate the module cache to avoid surprising
    cross-format collisions; callers that need caching should call
    :func:`load_toolset` instead.
    """
    # Determine preferred format
    prefer_yaml = _is_small_model(model)

    # Candidate paths
    yaml_path = _DIR / f"{name}.yaml"
    yml_path = _DIR / f"{name}.yml"
    json_path = _DIR / f"{name}.json"

    # Preference order
    candidates = (
        [yaml_path, yml_path, json_path]
        if prefer_yaml
        else [json_path, yaml_path, yml_path]
    )

    dir_key = str(_DIR)

    for p in candidates:
        try:
            fmt = "yaml" if p.suffix.lower() in (".yaml", ".yml") else "json"

            # Check the format-aware cache first (keyed by name, format and dir)
            cache_key = (name, fmt, dir_key)
            if cache_key in _format_cache:
                return _format_cache[cache_key]

            if not p.exists():
                continue
            with open(p, "r", encoding="utf-8") as f:
                toolset = yaml.safe_load(f) if fmt == "yaml" else json.load(f)
                # Populate the format-aware cache for future model-aware loads
                _format_cache[cache_key] = toolset
                return toolset
        except Exception:
            continue
    return None


def get_toolset_description(name: str) -> str:
    """Get the description of a toolset."""
    toolset = load_toolset(name)
    if toolset:
        return toolset.get("description", "")
    return ""
