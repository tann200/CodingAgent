"""Shared WorkspaceGuard class - single source of truth.

This module provides the WorkspaceGuard class that all tool modules use.
The real implementation lives in src.core.orchestration.workspace_guard.
When the core module is unavailable (e.g., during testing), a no-op fallback is used.
"""

import logging
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Union


class WorkspaceGuard:
    """Fail-closed guard when src.core is not available.

    When the real WorkspaceGuard cannot be imported we must NOT allow file
    operations to proceed unguarded. ``guard_operation`` returns an error so
    the calling tool blocks the operation (defense-in-depth: a missing guard
    must not grant write access).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

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


def check_file_safety(
    path: Union[str, Path],
    *,
    workdir: Optional[Path] = None,
    for_write: bool = False,
    allow_secret_override: bool = False,
) -> Optional[Dict[str, Any]]:
    """Defense-in-depth lexical + realpath file safety check (TASK-31).

    Checks:
      1. Git internals protection (all writes under ``.git/**``; reads of
         ``.git/{config,credentials,hooks/**,modules/**/config}``).
      2. Secret path protection on both lexical path and ``os.path.realpath()``.
      3. Dangling symlink write refusal (``is_symlink() and not exists()``).
      4. Multi-hardlink regular file protection (``st_nlink > 1`` via ``lstat``)
         to prevent hardlinking host credentials (e.g. ``~/.ssh/id_rsa``) into
         the workspace.

    Returns ``None`` when safe, or a structured error dict with ``reason_code``
    and ``recoverable=False`` when blocked.
    """
    from src.core.security.redaction import is_git_internals_path, is_secret_path as _is_sec

    raw_p = Path(path)
    lexical = raw_p if raw_p.is_absolute() else ((workdir or Path.cwd()) / raw_p)
    try:
        real = Path(os.path.realpath(str(lexical)))
    except Exception:
        real = lexical

    # 1. Git internals protection (both lexical and realpath)
    if is_git_internals_path(lexical, for_write=for_write) or is_git_internals_path(
        real, for_write=for_write
    ):
        return {
            "path": str(lexical),
            "status": "error",
            "error": (
                f"Access denied: '{raw_p}' targets protected git internals. "
                "Direct access to git config, credentials, hooks, or .git mutations is restricted."
            ),
            "reason_code": "git_internals_protected",
            "recoverable": False,
        }

    # 2. Secret path protection (both lexical and realpath)
    if not allow_secret_override and (_is_sec(lexical) or _is_sec(real)):
        return {
            "path": str(lexical),
            "status": "error",
            "error": (
                f"Protected secret file: '{raw_p.name}'. Access denied; "
                "reading or modifying credentials and private keys is restricted."
            ),
            "reason_code": "secret_file_protected",
            "recoverable": False,
        }

    # 3. Dangling symlink refusal on writes (before following symlink)
    try:
        if for_write and lexical.is_symlink() and not lexical.exists():
            return {
                "path": str(lexical),
                "status": "error",
                "error": f"Write denied: '{raw_p}' is a dangling symlink.",
                "reason_code": "dangling_symlink_protected",
                "recoverable": False,
            }
    except OSError:
        pass

    # 4. Hardlink laundering guard (lstat before open; refuse regular files with st_nlink > 1)
    for probe in (lexical, real):
        try:
            st = os.lstat(probe)
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                return {
                    "path": str(lexical),
                    "status": "error",
                    "error": (
                        f"Access denied: '{raw_p}' has multiple hardlinks (st_nlink={st.st_nlink}). "
                        "Multi-linked files are blocked to prevent credential laundering."
                    ),
                    "reason_code": "multi_link_file_protected",
                    "recoverable": False,
                }
        except OSError:
            continue

    return None
