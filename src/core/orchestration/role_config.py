from typing import Dict, Any, List, Optional

ROLE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "planner": {
        "description": "Breaks down complex tasks into structured plans with actionable steps.",
        "system_prompt_suffix": "Focus on decomposition and planning. Output structured plans.",
        "allowed_tools": [
            "search_code",
            "find_symbol",
            "find_references",
            "list_files",
            "read_file",
            "memory_search",
        ],
        "denied_tools": [
            "write_file",
            "edit_file",
            "delete_file",
            "run_tests",
            "apply_patch",
        ],
        "max_rounds": 20,
    },
    "coder": {
        "description": "Implements code changes based on plans from the planner.",
        "system_prompt_suffix": "Focus on code implementation. Use tools to read and modify files.",
        "allowed_tools": [
            "list_files",
            "read_file",
            "write_file",
            "edit_file",
            "delete_file",
            "search_code",
            "find_symbol",
            "run_tests",
            "run_linter",
            "syntax_check",
        ],
        "denied_tools": [],
        "max_rounds": 15,
    },
    "reviewer": {
        "description": "Validates code changes, runs tests, and ensures quality standards.",
        "system_prompt_suffix": "Focus on validation and quality. Check code correctness.",
        "allowed_tools": [
            "read_file",
            "list_files",
            "run_tests",
            "run_linter",
            "syntax_check",
            "search_code",
        ],
        "denied_tools": ["write_file", "edit_file", "delete_file", "apply_patch"],
        "max_rounds": 10,
    },
    "researcher": {
        "description": "Explores codebase, finds relevant code, and gathers context.",
        "system_prompt_suffix": "Focus on exploration and discovery. Find relevant code and patterns.",
        "allowed_tools": [
            "search_code",
            "find_symbol",
            "find_references",
            "list_files",
            "read_file",
            "memory_search",
            "analyze_repository",
            "initialize_repo_intelligence",
        ],
        "denied_tools": [
            "write_file",
            "edit_file",
            "delete_file",
            "apply_patch",
            "run_tests",
        ],
        "max_rounds": 12,
    },
}


# Canonical roles defined in docs/gap-analysis.md
CANONICAL_ROLES = ["analyst", "strategic", "operational", "reviewer", "debugger"]

# Map legacy/alternate role names to canonical roles
ROLE_ALIASES = {
    "planner": "strategic",
    "plan": "strategic",
    "planning": "strategic",
    "coder": "operational",
    "developer": "operational",
    "coding": "operational",
    "researcher": "analyst",
    "analysis": "analyst",
    "review": "reviewer",
    "audit": "reviewer",
    "debug": "debugger",
}

# Build canonical role configs by mapping existing ROLE_CONFIGS entries
CANONICAL_ROLE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "strategic": ROLE_CONFIGS.get("planner", {}),
    "operational": ROLE_CONFIGS.get("coder", {}),
    "reviewer": ROLE_CONFIGS.get("reviewer", {}),
    "analyst": ROLE_CONFIGS.get("researcher", {}),
    # debugger may not exist yet in ROLE_CONFIGS; provide a minimal placeholder
    "debugger": ROLE_CONFIGS.get("researcher", {}),
}


def normalize_role(role: str) -> str:
    """Normalize input role name to canonical role name.

    If the provided role is already canonical, return it. Otherwise map via
    ROLE_ALIASES. Default fallback is 'operational'.
    """
    if not role:
        return "operational"
    r = role.strip().lower()
    if r in CANONICAL_ROLES:
        return r
    return ROLE_ALIASES.get(r, "operational")


def get_role_config(role: str) -> Optional[Dict[str, Any]]:
    # normalize to canonical and return canonical config if available
    canonical = normalize_role(role)
    cfg = CANONICAL_ROLE_CONFIGS.get(canonical)
    if cfg:
        return cfg
    # fallback to legacy ROLE_CONFIGS if direct key was provided
    return ROLE_CONFIGS.get(role)


def get_role_system_prompt_suffix(role: str) -> str:
    config = get_role_config(role)
    if config:
        return config.get("system_prompt_suffix", "")
    return ""


def get_allowed_tools(role: str) -> List[str]:
    config = get_role_config(role)
    if config:
        return config.get("allowed_tools", [])
    return []


def get_denied_tools(role: str) -> List[str]:
    config = get_role_config(role)
    if config:
        return config.get("denied_tools", [])
    return []


def is_tool_allowed_for_role(tool_name: str, role: str) -> bool:
    denied = get_denied_tools(role)
    if tool_name in denied:
        return False
    allowed = get_allowed_tools(role)
    if allowed and tool_name not in allowed:
        return False
    return True


def list_roles() -> List[str]:
    """Return canonical roles to avoid overlaps in role naming."""
    return list(CANONICAL_ROLES)


def map_role_strict(role: str) -> Optional[str]:
    """Map role to canonical if it is known or an alias; return None if unknown."""
    if not role:
        return None
    r = role.strip().lower()
    if r in CANONICAL_ROLES:
        return r
    if r in ROLE_ALIASES:
        return ROLE_ALIASES[r]
    return None


class RoleManager:
    def __init__(self):
        self._current_role: Optional[str] = None
        self._role_history: List[Dict[str, Any]] = []

    def set_role(self, role: str) -> bool:
        # use strict mapping here: unknown role strings should fail
        canonical = map_role_strict(role)
        if canonical is None:
            return False
        if canonical not in CANONICAL_ROLES:
            return False
        self._current_role = canonical
        self._role_history.append({"role": canonical})
        return True

    def get_current_role(self) -> Optional[str]:
        return self._current_role

    def get_role_config(self) -> Optional[Dict[str, Any]]:
        if self._current_role:
            return CANONICAL_ROLE_CONFIGS.get(self._current_role)
        return None

    def get_allowed_tools(self) -> List[str]:
        if self._current_role:
            return get_allowed_tools(self._current_role)
        return []

    def get_denied_tools(self) -> List[str]:
        if self._current_role:
            return get_denied_tools(self._current_role)
        return []

    def is_tool_allowed(self, tool_name: str) -> bool:
        if not self._current_role:
            return True
        return is_tool_allowed_for_role(tool_name, self._current_role)
