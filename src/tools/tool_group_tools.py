"""On-demand optional tool-group loader (TASK-44, inspired by haze loadToolGroup).

Keeps default flat toolsets lean while allowing frontier/standard sessions to
unlock specialist tool groups (``git-write``, ``memory``, ``ast``) on demand
at zero idle schema cost.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Tuple

from src.tools._tool import PermissionKind, tool

ToolGroupName = Literal["git-write", "memory", "ast"]

TOOL_GROUPS: Dict[str, Tuple[str, ...]] = {
    "git-write": ("git_commit", "git_stash", "git_restore"),
    "memory": ("memory_search", "memory_save"),
    "ast": ("ast_list_symbols", "ast_rename"),
}


def get_tools_for_group(group: str) -> Tuple[str, ...]:
    """Return the tool names belonging to *group*, or an empty tuple."""
    return TOOL_GROUPS.get((group or "").strip().lower(), ())


@tool(tags=["coding"], permission_kind=PermissionKind.NONE)
def load_tool_group(
    group: ToolGroupName,
) -> Dict[str, Any]:
    """Load an optional specialist tool group for subsequent turns.

    Groups:
      - ``git-write``: ``git_commit``, ``git_stash``, ``git_restore``
      - ``memory``: ``memory_search``, ``memory_save`` (disabled in ``--private`` mode)
      - ``ast``: ``ast_list_symbols``, ``ast_rename``
    """
    key = (group or "").strip().lower()
    tools = TOOL_GROUPS.get(key)
    if not tools:
        return {
            "status": "error",
            "error": (
                f"Unknown tool group {group!r}. "
                f"Available groups: {', '.join(sorted(TOOL_GROUPS))}."
            ),
        }

    if key == "memory":
        from src.core.context.tool_prompt_sections import is_private_mode_enabled

        if is_private_mode_enabled():
            return {
                "status": "error",
                "error": "Tool group 'memory' is disabled in private session mode (CODINGAGENT_PRIVATE=1).",
            }

    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        orch = _PARENT_ORCHESTRATOR_VAR.get(None)
        if orch is not None:
            loaded = getattr(orch, "_loaded_tool_groups", None)
            if not isinstance(loaded, set):
                loaded = set()
                setattr(orch, "_loaded_tool_groups", loaded)
            already_loaded = key in loaded
            loaded.add(key)
        else:
            already_loaded = False
    except Exception:
        already_loaded = False

    return {
        "status": "ok",
        "group": key,
        "already_loaded": already_loaded,
        "tools": list(tools),
        "message": f"Loaded tool group '{key}' ({', '.join(tools)}); available on the next turn.",
    }
