"""Role capability configuration and legacy-role normalization (TASK-33).

CodingAgent runs a single-agent + progressive-disclosure skills architecture
with opt-in read-only ``scout`` and isolated ``worktree`` delegation. Legacy
role names (planner, coder, reviewer, researcher, debugger, tester, general)
remain supported as normalized permission presets.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_READ_CORE: tuple[str, ...] = (
    "read_file", "read_tool_output", "list_files", "glob", "grep",
    "search_code", "find_symbol", "find_references", "memory_search",
)
_READ_EXTENDED: tuple[str, ...] = (
    *_READ_CORE, "bash_readonly", "git_read", "git_log", "git_diff", "git_status",
    "lsp", "lsp_diagnostics", "lsp_references", "lsp_definition",
    "analyze_repository", "initialize_repo_intelligence",
    "multi_file_summary", "batched_file_read", "load_skill",
)
_WRITE_DENIED: tuple[str, ...] = (
    "write_file", "edit_file", "edit_file_atomic", "edit_by_line_range",
    "multiedit", "delete_file", "apply_patch", "generate_patch",
    "bash", "run_tests", "run_js_tests", "run_linter", "run_ts_check",
    "delegate_task", "git_commit",
)
_IMPL_CORE: tuple[str, ...] = (
    *_READ_CORE, "write_file", "edit_file", "edit_file_atomic",
    "edit_by_line_range", "multiedit", "apply_patch", "delete_file",
    "bash", "check_background_task", "run_tests", "run_js_tests",
    "run_linter", "run_ts_check", "syntax_check",
    "git_read", "git_status", "git_diff", "git_log", "manage_todo", "ask_user",
    "delegate_task", "lsp", "lsp_diagnostics", "lsp_references", "lsp_definition",
    "web_search", "read_web_page", "load_skill", "load_tool_group", "list_skills",
)


ROLE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "planner": {
        "description": "Breaks down complex tasks into structured plans with actionable steps.",
        "system_prompt_suffix": "Focus on decomposition and planning. Output structured plans.",
        "allowed_tools": list(_READ_CORE),
        "denied_tools": ["write_file", "edit_file", "delete_file", "run_tests", "apply_patch"],
        "max_rounds": 20,
    },
    "coder": {
        "description": "Implements code changes based on plans from the planner.",
        "system_prompt_suffix": "Focus on code implementation. Use tools to read and modify files.",
        "allowed_tools": list(_IMPL_CORE),
        "denied_tools": [],
        "max_rounds": 15,
    },
    "reviewer": {
        "description": "Validates code changes, runs tests, and ensures quality standards.",
        "system_prompt_suffix": "Focus on validation and quality. Check code correctness.",
        "allowed_tools": ["read_file", "read_tool_output", "list_files", "run_tests", "run_linter", "syntax_check", "search_code", "load_skill"],
        "denied_tools": ["write_file", "edit_file", "delete_file", "apply_patch"],
        "max_rounds": 10,
    },
    "researcher": {
        "description": "Explores codebase, finds relevant code, and gathers context.",
        "system_prompt_suffix": "Focus on exploration and discovery. Find relevant code and patterns.",
        "allowed_tools": list(_READ_EXTENDED),
        "denied_tools": list(_WRITE_DENIED),
        "max_rounds": 12,
    },
    "analyst_role": {
        "description": "Read-only codebase exploration for pre-planning intelligence gathering.",
        "system_prompt_suffix": "You may only read files and search. Never write, edit, delete, or run tests.",
        "allowed_tools": list(_READ_EXTENDED),
        "denied_tools": list(_WRITE_DENIED),
        "max_rounds": 15,
    },
    "debugger_role": {
        "description": "Diagnoses failures and applies minimal targeted fixes.",
        "system_prompt_suffix": "Fix only what is broken. Read before editing. Verify with tests.",
        "allowed_tools": [t for t in _IMPL_CORE if t not in ("delete_file", "delegate_task")],
        "denied_tools": ["delete_file", "delegate_task"],
        "max_rounds": 12,
    },
    "scout": {
        "description": "Rapid read-only codebase exploration — finds files, patterns, and dependencies.",
        "system_prompt_suffix": "Explore the codebase quickly. Read files, search patterns, report findings. Never write or modify.",
        "allowed_tools": list(_READ_EXTENDED),
        "denied_tools": [*_WRITE_DENIED, "ask_user", "submit_plan_for_review", "manage_todo"],
        "max_rounds": 12,
    },
    "tester": {
        "description": "Test creation and execution — writes tests, runs suites, reports coverage.",
        "system_prompt_suffix": "Write and run tests. Read implementation code first, then write comprehensive tests.",
        "allowed_tools": [t for t in _IMPL_CORE if t not in ("delete_file", "apply_patch", "delegate_task")],
        "denied_tools": ["delete_file", "apply_patch", "generate_patch", "delegate_task", "ask_user", "submit_plan_for_review", "git_commit", "manage_todo"],
        "max_rounds": 15,
    },
    "general_role": {
        "description": "Full-access workhorse for parallel research and execution tasks.",
        "system_prompt_suffix": "You have full tool access. Read, write, search, and execute as needed.",
        "allowed_tools": list(_IMPL_CORE),
        "denied_tools": [],
        "max_rounds": 15,
    },
}

CANONICAL_ROLES: List[str] = [
    "analyst", "strategic", "operational", "reviewer", "debugger", "general",
    "scout", "tester", "explore", "verification",
]

ROLE_ALIASES: Dict[str, str] = {
    "planner": "strategic", "plan": "strategic", "planning": "strategic",
    "coder": "operational", "developer": "operational", "coding": "operational",
    "researcher": "analyst", "analysis": "analyst",
    "review": "reviewer", "audit": "reviewer",
    "debug": "debugger",
    "generalist": "general", "general_purpose": "general",
    "explorer": "explore", "verify": "verification", "verifier": "verification",
}

CANONICAL_ROLE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "strategic": ROLE_CONFIGS["planner"],
    "operational": ROLE_CONFIGS["coder"],
    "reviewer": ROLE_CONFIGS["reviewer"],
    "analyst": ROLE_CONFIGS["analyst_role"],
    "debugger": ROLE_CONFIGS["debugger_role"],
    "scout": ROLE_CONFIGS["scout"],
    "tester": ROLE_CONFIGS["tester"],
    "general": ROLE_CONFIGS["general_role"],
}


def _populate_agent_type_roles() -> None:
    try:
        from src.core.orchestration.agent_types import get_agent as _get_agent

        for role_id in ("explore", "verification"):
            if role_id in CANONICAL_ROLE_CONFIGS:
                continue
            agent_def = _get_agent(role_id)
            if agent_def is not None:
                CANONICAL_ROLE_CONFIGS[role_id] = {
                    "description": agent_def.description,
                    "system_prompt_suffix": "",
                    "allowed_tools": sorted(agent_def.allowed_tools) if agent_def.allowed_tools is not None else [],
                    "denied_tools": sorted(agent_def.denied_tools),
                    "max_rounds": agent_def.max_rounds,
                }
    except Exception:
        pass


_populate_agent_type_roles()


def normalize_role(role: str) -> str:
    """Normalize input role name to canonical role name (default: 'operational')."""
    if not role:
        return "operational"
    r = role.strip().lower()
    return r if r in CANONICAL_ROLES else ROLE_ALIASES.get(r, "operational")


def get_role_config(role: str) -> Optional[Dict[str, Any]]:
    canonical = normalize_role(role)
    return CANONICAL_ROLE_CONFIGS.get(canonical) or ROLE_CONFIGS.get(role)


def get_role_system_prompt_suffix(role: str) -> str:
    cfg = get_role_config(role)
    return str(cfg.get("system_prompt_suffix", "")) if cfg else ""


def get_allowed_tools(role: str) -> List[str]:
    cfg = get_role_config(role)
    return list(cfg.get("allowed_tools", [])) if cfg else []


def get_denied_tools(role: str) -> List[str]:
    cfg = get_role_config(role)
    return list(cfg.get("denied_tools", [])) if cfg else []


def is_tool_allowed_for_role(tool_name: str, role: str) -> bool:
    if tool_name in get_denied_tools(role):
        return False
    allowed = get_allowed_tools(role)
    return not allowed or tool_name in allowed


def list_roles() -> List[str]:
    return list(CANONICAL_ROLES)


def map_role_strict(role: str) -> Optional[str]:
    if not role:
        return None
    r = role.strip().lower()
    if r in CANONICAL_ROLES:
        return r
    return ROLE_ALIASES.get(r)


class RoleManager:
    def __init__(self) -> None:
        self._current_role: Optional[str] = None
        self._role_history: List[Dict[str, Any]] = []

    def set_role(self, role: str) -> bool:
        canonical = map_role_strict(role)
        if canonical is None or canonical not in CANONICAL_ROLES:
            return False
        self._current_role = canonical
        self._role_history.append({"role": canonical})
        return True

    def get_current_role(self) -> Optional[str]:
        return self._current_role

    def get_role_config(self) -> Optional[Dict[str, Any]]:
        return CANONICAL_ROLE_CONFIGS.get(self._current_role) if self._current_role else None

    def get_allowed_tools(self) -> List[str]:
        return get_allowed_tools(self._current_role) if self._current_role else []

    def get_denied_tools(self) -> List[str]:
        return get_denied_tools(self._current_role) if self._current_role else []

    def is_tool_allowed(self, tool_name: str) -> bool:
        return True if not self._current_role else is_tool_allowed_for_role(tool_name, self._current_role)


_ROLE_PREFERS_SMALL_MODEL: Dict[str, bool] = {
    "analyst": True, "strategic": True, "reviewer": True, "scout": True,
    "operational": False, "debugger": False, "tester": False, "general": False,
}


def get_default_model_for_role(role: str) -> Optional[str]:
    """Return configured ``small_model`` for read-heavy roles, else ``None``."""
    if not _ROLE_PREFERS_SMALL_MODEL.get(normalize_role(role), False):
        return None
    try:
        from src.core.config_loader import get_small_model as _gsm

        return _gsm()
    except Exception:
        return None
