"""Shared WorkspaceGuard class - single source of truth.

This module provides the WorkspaceGuard class that all tool modules use.
The real implementation lives in src.core.orchestration.workspace_guard.
When the core module is unavailable (e.g., during testing), a no-op fallback is used.
"""

from typing import Dict


class WorkspaceGuard:
    """No-op guard when src.core is not available."""

    def guard_operation(self, *args: object, **kwargs: object) -> Dict[str, str]:
        """No-op operation - always returns ok."""
        return {"status": "ok"}


try:
    from src.core.orchestration.workspace_guard import WorkspaceGuard as _WorkspaceGuard  # noqa: F401
    # Replace the stub with the real implementation if available
    WorkspaceGuard = _WorkspaceGuard  # type: ignore[assignment]
except ImportError:
    pass
