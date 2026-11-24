"""Tool registry compatibility re-export & per-tool timeout map.

TASK-22: ``src.tools._registry.ToolRegistry`` is now the sole authoritative
``ToolRegistry`` class.  This module retains the canonical per-tool timeout map
(``get_tool_timeout``) and re-exports ``ToolRegistry`` for backwards-compatible
imports across orchestration and tests.
"""

from __future__ import annotations

from typing import Dict

from src.tools._registry import ToolRegistry  # noqa: F401

# ---------------------------------------------------------------------------
# Per-tool timeout map (seconds).  0 means no timeout.
# T9: Tool Timeout Protection — values here are the canonical source of truth.
# ---------------------------------------------------------------------------

_TOOL_TIMEOUTS: Dict[str, int] = {
    "bash": 60,
    "run_tests": 120,
    "run_linter": 60,
    "syntax_check": 30,
    "run_js_tests": 120,
    "run_ts_check": 120,
    "run_eslint": 60,
    "search_code": 30,
    "grep": 30,
    "edit_file_atomic": 30,
    "find_symbol": 30,
    "list_files": 10,
    "glob": 10,
}

#: Default timeout used for any tool not listed in ``_TOOL_TIMEOUTS``.
DEFAULT_TOOL_TIMEOUT: int = 30


def get_tool_timeout(tool_name: str) -> int:
    """Return the execution timeout in seconds for *tool_name*.

    Falls back to :data:`DEFAULT_TOOL_TIMEOUT` (30 s) for unknown tools.
    """
    return _TOOL_TIMEOUTS.get(tool_name, DEFAULT_TOOL_TIMEOUT)


__all__ = ["ToolRegistry", "DEFAULT_TOOL_TIMEOUT", "_TOOL_TIMEOUTS", "get_tool_timeout"]
