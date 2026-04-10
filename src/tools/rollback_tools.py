"""Filesystem snapshot / undo tools.

These tools let the agent revert the most recent write-tool call (FS-1) or
inspect available snapshots (FS-2).

The snapshot infrastructure lives in ``RollbackManager`` (already wired into
``Orchestrator.execute_tool()``).  The tools here provide a callable interface
for the LLM — no manual orchestrator coupling required.

Design notes
------------
* ``revert_last_tool``  — reverts the snapshot taken immediately before the
  last write tool call (``orchestrator._current_snapshot_id``).
* ``list_snapshots``    — shows all available snapshots with metadata.

Both tools access the active orchestrator via the ``_PARENT_ORCHESTRATOR_VAR``
ContextVar (set by ``Orchestrator.execute_tool`` before every dispatch).  If no
orchestrator is available the tools return a descriptive error rather than
crashing, so they degrade gracefully in unit-test contexts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.tools._tool import tool

logger = logging.getLogger(__name__)

# FS-1 / FS-2: write-family tools whose pre-call snapshots are revertable
WRITE_TOOL_NAMES = frozenset(
    {
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "apply_patch",
        "delete_file",
    }
)


def _get_orchestrator() -> Optional[Any]:
    """Return the active orchestrator from the ContextVar, or None."""
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        return _PARENT_ORCHESTRATOR_VAR.get(None)
    except Exception:
        return None


@tool(tags=["filesystem", "safety"])
def revert_last_tool() -> Dict[str, Any]:
    """Revert the filesystem changes made by the last write tool call.

    Uses the snapshot taken immediately before the most recent write tool
    (write_file, edit_file, edit_file_atomic, apply_patch, delete_file, etc.)
    to restore all affected files to their pre-call state.

    Useful when a write tool produced an incorrect result and you want to undo
    it before retrying.  Only the *last* write tool call is reverted —
    earlier writes remain in place.

    Returns:
        Dict with keys:
            ok            — True if rollback succeeded, False otherwise
            snapshot_id   — ID of the snapshot that was restored
            restored_files — List of file paths restored
            restored_count — Number of files restored
            error         — Error message if ok is False
    """
    orch = _get_orchestrator()
    if orch is None:
        return {
            "ok": False,
            "error": "revert_last_tool: no active orchestrator context — cannot revert.",
        }

    snapshot_id: Optional[str] = getattr(orch, "_current_snapshot_id", None)
    if not snapshot_id:
        return {
            "ok": False,
            "error": (
                "No snapshot available for revert. "
                "Either no write tool has been called yet, or the previous snapshot "
                "was already consumed by a prior revert."
            ),
        }

    rollback_mgr = getattr(orch, "rollback_manager", None)
    if rollback_mgr is None:
        return {
            "ok": False,
            "error": "revert_last_tool: RollbackManager not initialised on orchestrator.",
        }

    result = rollback_mgr.rollback(snapshot_id)

    # FS-1: Clear the snapshot reference so a second revert_last_tool call
    # doesn't silently re-apply the same rollback.
    if result.get("ok"):
        orch._current_snapshot_id = None
        logger.info("revert_last_tool: reverted snapshot %s", snapshot_id)

    return result


@tool(tags=["filesystem", "safety"])
def list_snapshots() -> Dict[str, Any]:
    """List all available filesystem snapshots that can be reverted.

    Each snapshot records the pre-write state of one or more files captured
    immediately before a write tool call.

    Returns:
        Dict with keys:
            ok        — True
            snapshots — List of snapshot dicts, each with:
                          snapshot_id  — unique snapshot identifier
                          timestamp    — ISO-8601 creation time
                          file_count   — number of files captured
            current_snapshot_id — ID of the snapshot that revert_last_tool
                                   would restore (may be None)
    """
    orch = _get_orchestrator()
    if orch is None:
        return {
            "ok": False,
            "error": "list_snapshots: no active orchestrator context.",
        }

    rollback_mgr = getattr(orch, "rollback_manager", None)
    if rollback_mgr is None:
        return {
            "ok": False,
            "error": "list_snapshots: RollbackManager not initialised on orchestrator.",
        }

    snapshots: List[Dict[str, Any]] = rollback_mgr.list_snapshots()
    current_id: Optional[str] = getattr(orch, "_current_snapshot_id", None)

    return {
        "ok": True,
        "snapshots": snapshots,
        "current_snapshot_id": current_id,
        "revertable": current_id is not None,
    }
