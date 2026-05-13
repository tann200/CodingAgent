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
    # Analyst role: read-only exploration (mirrors opencode's 'explore' agent).
    # No file writes, no test execution, no task delegation — pure reconnaissance.
    "analyst_role": {
        "description": "Read-only codebase exploration for pre-planning intelligence gathering.",
        "system_prompt_suffix": "You may only read files and search. Never write, edit, delete, or run tests.",
        "allowed_tools": [
            "read_file",
            "list_files",
            "glob",
            "grep",
            "search_code",
            "find_symbol",
            "find_references",
            "bash",
            "git_log",
            "git_diff",
            "git_status",
            "memory_search",
            "analyze_repository",
            "initialize_repo_intelligence",
            "multi_file_summary",
            "batched_file_read",
        ],
        "denied_tools": [
            "write_file",
            "edit_file",
            "edit_file_atomic",
            "edit_by_line_range",
            "delete_file",
            "apply_patch",
            "run_tests",
            "run_js_tests",
            "run_linter",
            "run_ts_check",
            "delegate_task",
        ],
        "max_rounds": 15,
    },
    # Debugger role: full access — reads, edits, tests, but no delegation.
    "debugger_role": {
        "description": "Diagnoses failures and applies minimal targeted fixes.",
        "system_prompt_suffix": "Fix only what is broken. Read before editing. Verify with tests.",
        "allowed_tools": [
            "read_file",
            "list_files",
            "glob",
            "grep",
            "search_code",
            "find_symbol",
            "find_references",
            "bash",
            "edit_file",
            "edit_file_atomic",
            "edit_by_line_range",
            "write_file",
            "run_tests",
            "run_js_tests",
            "run_linter",
            "run_ts_check",
            "git_log",
            "git_diff",
        ],
        "denied_tools": [
            "delete_file",
            "delegate_task",
        ],
        "max_rounds": 12,
    },
    # Scout role: read-only rapid codebase exploration.
    # Matches the scout brain file and PRSW READ_ONLY_ROLES classification.
    "scout": {
        "description": "Rapid codebase exploration — finds files, patterns, and dependencies.",
        "system_prompt_suffix": "Explore the codebase quickly. Read files, search patterns, report findings. Never write or modify.",
        "allowed_tools": [
            "read_file",
            "list_files",
            "glob",
            "grep",
            "search_code",
            "find_symbol",
            "find_references",
            "bash_readonly",
            "memory_search",
            "analyze_repository",
            "initialize_repo_intelligence",
            "multi_file_summary",
            "batched_file_read",
        ],
        "denied_tools": [
            "write_file",
            "edit_file",
            "edit_file_atomic",
            "edit_by_line_range",
            "multiedit",
            "delete_file",
            "apply_patch",
            "generate_patch",
            "bash",
            "run_tests",
            "run_js_tests",
            "run_linter",
            "run_ts_check",
            "delegate_task",
            "ask_user",
            "submit_plan_for_review",
            "git_commit",
            "manage_todo",
        ],
        "max_rounds": 12,
    },
    # Tester role: test creation and execution.
    # Matches the tester brain file and PRSW WRITE_ROLES classification.
    "tester": {
        "description": "Test creation and execution — writes tests, runs suites, reports coverage.",
        "system_prompt_suffix": "Write and run tests. Read implementation code first, then write comprehensive tests. Execute and report results.",
        "allowed_tools": [
            "read_file",
            "list_files",
            "glob",
            "grep",
            "search_code",
            "find_symbol",
            "find_references",
            "bash",
            "write_file",
            "edit_file",
            "run_tests",
            "run_js_tests",
            "run_linter",
            "run_ts_check",
            "syntax_check",
            "git_log",
            "git_diff",
            "git_status",
            "batched_file_read",
            "multi_file_summary",
        ],
        "denied_tools": [
            "delete_file",
            "apply_patch",
            "generate_patch",
            "delegate_task",
            "ask_user",
            "submit_plan_for_review",
            "git_commit",
            "manage_todo",
        ],
        "max_rounds": 15,
    },
    # General role: full-access workhorse for parallel research+execution.
    # Mirrors opencode's 'general' subagent type (agent.ts:146-159).
    "general_role": {
        "description": "Full-access workhorse for parallel research and execution tasks.",
        "system_prompt_suffix": "You have full tool access. Read, write, search, and execute as needed.",
        "allowed_tools": [
            "read_file",
            "list_files",
            "glob",
            "grep",
            "search_code",
            "find_symbol",
            "find_references",
            "batched_file_read",
            "multi_file_summary",
            "bash",
            "write_file",
            "edit_file",
            "edit_file_atomic",
            "edit_by_line_range",
            "delete_file",
            "apply_patch",
            "run_tests",
            "run_linter",
            "run_ts_check",
            "git_log",
            "git_diff",
            "git_status",
            "web_search",
            "read_web_page",
            "memory_search",
            "analyze_repository",
            "initialize_repo_intelligence",
            "load_skill",
            "list_skills",
        ],
        "denied_tools": [],
        "max_rounds": 15,
    },
}


# Canonical roles defined in docs/gap-analysis.md
CANONICAL_ROLES = [
    "analyst", "strategic", "operational", "reviewer", "debugger", "general",
    "scout", "tester",
]

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
    "generalist": "general",
    "general_purpose": "general",
}

# Build canonical role configs by mapping existing ROLE_CONFIGS entries
CANONICAL_ROLE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "strategic": ROLE_CONFIGS.get("planner", {}),
    "operational": ROLE_CONFIGS.get("coder", {}),
    "reviewer": ROLE_CONFIGS.get("reviewer", {}),
    # analyst: read-only explore mode (mirrors opencode's 'explore' agent)
    "analyst": ROLE_CONFIGS.get("analyst_role", ROLE_CONFIGS.get("researcher", {})),
    # debugger: full edit+test access, no deletion/delegation
    "debugger": ROLE_CONFIGS.get("debugger_role", ROLE_CONFIGS.get("researcher", {})),
    # scout: read-only codebase exploration
    "scout": ROLE_CONFIGS.get("scout", {}),
    # tester: test creation and execution
    "tester": ROLE_CONFIGS.get("tester", {}),
    # general: full-access workhorse for parallel research+execution
    "general": ROLE_CONFIGS.get("general_role", ROLE_CONFIGS.get("coder", {})),
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


# ---------------------------------------------------------------------------
# SM-1: Role-to-model default binding
# ---------------------------------------------------------------------------
# Read-heavy roles (analyst, reviewer, strategic) default to the configured
# small_model so they don't consume frontier-model tokens for lightweight tasks.
# None means "use the active provider's default model" (no override injected).
#
# Users / callers can always override this by passing model= to delegate_task.
_ROLE_PREFERS_SMALL_MODEL: Dict[str, bool] = {
    "analyst": True,  # read-only exploration — small model sufficient
    "strategic": True,  # planning / decomposition — reasoning, not code gen
    "reviewer": True,  # code review — read-heavy, no complex generation
    "operational": False,  # code implementation — needs full model capability
    "debugger": False,  # root-cause + editing — needs full model capability
    "scout": True,  # read-only exploration — small model sufficient
    "tester": False,  # test writing + execution — needs full model capability
    "general": False,  # full-access workhorse — needs full model capability
}


def get_default_model_for_role(role: str) -> Optional[str]:
    """SM-1: Return the default model name for *role*.

    Returns the configured ``small_model`` for read-heavy roles and ``None``
    (use the active provider's default) for implementation/debug roles.

    Parameters
    ----------
    role:
        Canonical or legacy role name.

    Returns
    -------
    str or None
        Model name string, or ``None`` to use the provider default.
    """
    canonical = normalize_role(role)
    prefers_small = _ROLE_PREFERS_SMALL_MODEL.get(canonical, False)
    if not prefers_small:
        return None
    try:
        from src.core.config_loader import get_small_model as _gsm

        return _gsm()
    except Exception:
        return None
