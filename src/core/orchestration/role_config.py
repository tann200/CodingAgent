from typing import Dict, Any, List, Optional
from pathlib import Path

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


def get_role_config(role: str) -> Optional[Dict[str, Any]]:
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
    return list(ROLE_CONFIGS.keys())


class RoleManager:
    def __init__(self):
        self._current_role: Optional[str] = None
        self._role_history: List[Dict[str, Any]] = []

    def set_role(self, role: str) -> bool:
        if role not in ROLE_CONFIGS:
            return False
        self._current_role = role
        self._role_history.append({"role": role})
        return True

    def get_current_role(self) -> Optional[str]:
        return self._current_role

    def get_role_config(self) -> Optional[Dict[str, Any]]:
        if self._current_role:
            return ROLE_CONFIGS.get(self._current_role)
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
