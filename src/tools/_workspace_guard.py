"""Shared WorkspaceGuard class - single source of truth.

This module provides the WorkspaceGuard class that all tool modules use.
The real implementation lives in src.core.orchestration.workspace_guard.
When the core module is unavailable (e.g., during testing), a no-op fallback is used.
"""

import logging
from typing import Dict


class WorkspaceGuard:
    """Fail-closed guard when src.core is not available.

    When the real WorkspaceGuard cannot be imported we must NOT allow file
    operations to proceed unguarded. ``guard_operation`` returns an error so
    the calling tool blocks the operation (defense-in-depth: a missing guard
    must not grant write access).
    """

    def guard_operation(self, *args: object, **kwargs: object) -> Dict[str, str]:
        """Fail-closed operation - always blocks with an error."""
        return {
            "status": "error",
            "error": "WorkspaceGuard unavailable: security checks could not be "
            "loaded, so this operation was blocked.",
        }


try:
    from src.core.orchestration.workspace_guard import WorkspaceGuard as WorkspaceGuard  # type: ignore[assignment]  # noqa: F401
except ImportError:
    logging.getLogger(__name__).warning(
        "Could not import real WorkspaceGuard from src.core.orchestration.workspace_guard; "
        "using no-op fallback. Security checks will be bypassed."
    )
