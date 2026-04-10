"""tool_constants.py — Shared tool-classification sets and permission audit helper.

These constants are used by ``orchestrator.py``, ``permission_gateway.py``, and
``loop_guards.py``.  Keeping them in a leaf module (no internal imports) eliminates
the circular dependency where ``permission_gateway`` previously had to import back
from ``orchestrator``.

Phase A of the orchestrator refactoring plan.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Tools that require the target file to have been read in the current session before writing
WRITE_TOOLS_REQUIRING_READ: frozenset = frozenset(
    {
        "edit_file",
        "edit_file_atomic",
        "write_file",
        "edit_by_line_range",
        "apply_patch",
        # Destructive tools — also subject to the _affected_files scope guard
        "delete_file",
        "rename_file",
        "ast_rename",
        "manage_todo",  # SEC-2: sync with MODIFYING_TOOLS in loop_guards
    }
)

# UX-3: Additional tools blocked in dry-run mode beyond WRITE_TOOLS_REQUIRING_READ.
# These are tools that execute side-effects (bash, network ops) that cannot be
# trivially previewed.
DRY_RUN_BLOCKED_TOOLS: frozenset = frozenset(
    WRITE_TOOLS_REQUIRING_READ
    | {
        "bash",
        "run_bash",
        "execute_bash",
        "run_command",
        "execute_command",
        "git_commit",
        "git_push",
    }
)

# Tools that always require explicit user approval before execution
PERMISSION_REQUIRED_TOOLS: frozenset = frozenset(
    {
        "delete_file",
        "run_bash",
    }
)


def _write_permission_audit(
    working_dir: Any,
    tool_name: str,
    args: dict,
    decision: str,
    reason: str = "",
) -> None:
    """PERM-W5: Append a permission audit entry to .agent/permission_audit.jsonl.

    Each line is a JSON object with fields: timestamp, tool, decision, reason.
    The file uses append-only JSONL so it never grows unboundedly per-process
    (a new process or session resets nothing — the file accumulates across runs).
    Args are not logged to avoid leaking sensitive values.
    """
    try:
        wd = Path(str(working_dir)) if working_dir else Path.cwd()
        audit_path = wd / ".agent" / "permission_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps(
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "tool": tool_name,
                "decision": decision,
                "reason": reason,
            }
        )
        with audit_path.open("a", encoding="utf-8") as _f:
            _f.write(entry + "\n")
    except Exception:
        pass  # audit failures must never block tool execution
