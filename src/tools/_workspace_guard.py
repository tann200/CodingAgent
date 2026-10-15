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

    def is_secret(self, path: object) -> bool:
        """Fail-closed secret check."""
        return True

    def guard_operation(self, *args: object, **kwargs: object) -> Dict[str, str]:
        """Fail-closed operation - always blocks with an error."""
        return {
            "status": "error",
            "error": "WorkspaceGuard unavailable: security checks could not be "
            "loaded, so this operation was blocked.",
        }


def is_secret_path(path: object) -> bool:
    """Fail-closed fallback for is_secret_path."""
    return True


try:
    from src.core.orchestration.workspace_guard import (  # type: ignore[assignment]  # noqa: F401
        WorkspaceGuard as WorkspaceGuard,
        is_secret_path as is_secret_path,
    )
except ImportError:
    logging.getLogger(__name__).warning(
        "Could not import real WorkspaceGuard from src.core.orchestration.workspace_guard; "
        "using no-op fallback. Security checks will be bypassed."
    )
