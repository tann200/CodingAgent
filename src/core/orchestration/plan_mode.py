"""
Plan Mode for blocking write tools until plan approval.
"""

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class PlanMode:
    """Manages plan-first development mode."""

    BLOCKED_TOOLS = {
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "multiedit",
        "write_file",
        "create_file",
        "delete_file",
        "rename_file",
        "move_file",
        "apply_patch",
        "ast_rename",
        "lsp_rename",
        "format_file",
        "create_directory",
        "mkdir",
        "bash",
        "run_bash",
        "git_commit",
        "git_add",
        "git_restore",
        "git_stash",
        "files_write",
        "files_edit",
        "files_delete",
        "files_move",
        "files_patch",
    }

    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator
        self.enabled = False
        self.pending_plan: Optional[Dict[str, Any]] = None

    def enable(self):
        """Enable plan mode - blocks write tools."""
        self.enabled = True
        self.pending_plan = None
        logger.info("PlanMode: enabled")

    def disable(self):
        """Disable plan mode after approval."""
        self.enabled = False
        self.pending_plan = None
        logger.info("PlanMode: disabled")

    def set_pending_plan(self, plan: Dict[str, Any]):
        """Set the pending plan for approval."""
        self.pending_plan = plan

    def is_blocked(self, tool_name: str) -> bool:
        """Check if tool is blocked in plan mode."""
        return self.enabled and tool_name in self.BLOCKED_TOOLS

    def get_status(self) -> Dict[str, Any]:
        """Get current plan mode status."""
        return {
            "enabled": self.enabled,
            "has_pending_plan": self.pending_plan is not None,
            "blocked_tools": list(self.BLOCKED_TOOLS) if self.enabled else [],
        }
