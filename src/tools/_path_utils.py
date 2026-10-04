"""
Shared path-safety utilities used by file_tools, patch_tools, and others.

Extracted from file_tools.py (#29) so any tool module can import _safe_resolve
without pulling in the full file_tools dependency tree.
"""

from __future__ import annotations

import os
from pathlib import Path


def safe_resolve(path: str, workdir: Path | None) -> Path:
    """
    Safely resolve *path* relative to *workdir*, blocking path-traversal and
    symlink-escape attacks.

    - Relative paths are anchored to *workdir*.
    - When *workdir* is ``None``, ``Path.cwd()`` is used as the anchor.
    - ``strict=True`` resolve is attempted first so symlinks are followed fully;
      falls back to non-strict for not-yet-created files.
    - ``os.path.realpath`` is used as a second layer to catch symlinks that
      ``Path.resolve`` may not fully dereference on some platforms.

    Raises:
        PermissionError: if the resolved path escapes *workdir*.
    """
    if workdir is None:
        workdir = Path.cwd()
    p = Path(path)
    if not p.is_absolute():
        p = workdir / p

    try:
        p = p.resolve(strict=True)
    except FileNotFoundError:
        p = p.resolve()

    workdir_resolved = workdir.resolve()
    real_path = os.path.realpath(p)
    real_workdir = os.path.realpath(workdir_resolved)

    if not real_path.startswith(real_workdir + os.sep) and real_path != real_workdir:
        raise PermissionError(
            f"Path '{path}' resolves to '{real_path}' which is outside "
            f"working directory '{real_workdir}'. Symlink traversal blocked."
        )

    return p


def changed_paths(tool_name: str, tool_args: object) -> list[str]:
    """Return the file paths a modifying tool call may have changed.

    Single source of truth for "which paths did this tool touch" — the repo
    index refresh (``repo_context.refresh_index_after_write``) and the P3.2
    change-log journal both consume this, so the two can never disagree about
    what a tool touched.

    Aliases are resolved first (``write`` -> ``write_file``) so a rename cannot
    hide behind one.  ``rename_file`` yields *both* ends, because refreshing
    only the source leaves the destination's symbols stale.

    Args:
        tool_name: Tool name as invoked (alias form accepted).
        tool_args: The tool's arguments mapping; non-mappings yield ``[]``.

    Returns:
        De-duplicated path strings in preference order (empty if none).
    """
    if not isinstance(tool_args, dict):
        return []
    try:
        from src.tools.tools_config import resolve_tool_alias

        canonical = resolve_tool_alias(str(tool_name))
    except Exception:
        canonical = str(tool_name)

    keys: tuple[str, ...] = ("path", "file_path", "file")
    if canonical == "rename_file" or str(tool_name) == "rename_file":
        keys = ("path", "file_path", "file", "src_path", "old_path", "new_path")

    out: list[str] = []
    for key in keys:
        value = tool_args.get(key)
        if isinstance(value, str) and value.strip() and value not in out:
            out.append(value)
    return out
