"""
Shared path-safety utilities used by file_tools, patch_tools, and others.

Extracted from file_tools.py (#29) so any tool module can import _safe_resolve
without pulling in the full file_tools dependency tree.
"""
from __future__ import annotations

import os
from pathlib import Path


def safe_resolve(path: str, workdir: Path) -> Path:
    """
    Safely resolve *path* relative to *workdir*, blocking path-traversal and
    symlink-escape attacks.

    - Relative paths are anchored to *workdir*.
    - ``strict=True`` resolve is attempted first so symlinks are followed fully;
      falls back to non-strict for not-yet-created files.
    - ``os.path.realpath`` is used as a second layer to catch symlinks that
      ``Path.resolve`` may not fully dereference on some platforms.

    Raises:
        PermissionError: if the resolved path escapes *workdir*.
    """
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
