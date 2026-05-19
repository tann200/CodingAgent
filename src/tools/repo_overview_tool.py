"""repo_overview — on-demand repository directory tree + metadata snapshot.

Inspired by opencode's ``repo_overview`` tool (packages/opencode/src/tool/repo_overview.ts):
walks the working directory up to *depth* levels deep (default 3), caps at
*max_files* entries (default 200), and returns a structured overview that
planners can use for repo-aware decision making.

No upfront indexing is required.  The tool is cheap to call and designed to
be invoked lazily just before planning.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools._tool import tool, PermissionKind

# Directories always excluded from the walk
_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", "target",
    ".tox", ".eggs", "*.egg-info", ".cache", ".idea", ".DS_Store",
})

# Files that indicate project type
_MANIFEST_FILES: tuple[str, ...] = (
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Makefile", "CMakeLists.txt", "requirements.txt",
    "Pipfile", "poetry.lock",
)


def _walk_tree(
    root: Path,
    max_depth: int,
    max_files: int,
) -> List[Dict[str, Any]]:
    """Walk the directory tree and return a flat list of entry dicts."""
    entries: List[Dict[str, Any]] = []

    def _recurse(path: Path, depth: int) -> None:
        if depth > max_depth or len(entries) >= max_files:
            return
        try:
            children = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return

        for child in children:
            if len(entries) >= max_files:
                break
            name = child.name
            # Skip excluded directories
            if child.is_dir() and name in _EXCLUDE_DIRS:
                continue
            rel = str(child.relative_to(root))
            entry: Dict[str, Any] = {
                "path": rel,
                "type": "dir" if child.is_dir() else "file",
                "depth": depth,
            }
            if child.is_file():
                try:
                    entry["size"] = child.stat().st_size
                except OSError:
                    entry["size"] = -1
            entries.append(entry)
            if child.is_dir():
                _recurse(child, depth + 1)

    _recurse(root, 1)
    return entries


def _detect_project_type(root: Path) -> List[str]:
    """Return a list of detected project-type labels."""
    labels: List[str] = []
    for manifest in _MANIFEST_FILES:
        if (root / manifest).exists():
            labels.append(manifest)
    return labels


@tool(side_effects=[], tags=["repo", "planning"], permission_kind=PermissionKind.READ_FILE)
def repo_overview(
    workdir: Optional[str] = None,
    max_depth: int = 3,
    max_files: int = 200,
) -> Dict[str, Any]:
    """Return a lightweight directory tree and project metadata for *workdir*.

    Args:
        workdir: Root directory to inspect.  Defaults to the current working
            directory when omitted.
        max_depth: Maximum directory depth to traverse (default 3).
        max_files: Maximum total entries to include (default 200).

    Returns:
        A dict with keys:
          - ``root``: absolute path of the inspected directory
          - ``entries``: list of ``{path, type, depth[, size]}`` dicts
          - ``truncated``: True when the file cap was hit
          - ``manifests``: list of detected manifest/config filenames
          - ``total_entries``: count of entries before truncation cap
    """
    root = Path(workdir).resolve() if workdir else Path.cwd()
    if not root.is_dir():
        return {
            "ok": False,
            "error": f"Directory not found: {root}",
            "root": str(root),
            "entries": [],
            "truncated": False,
            "manifests": [],
        }

    entries = _walk_tree(root, max_depth=max_depth, max_files=max_files)
    manifests = _detect_project_type(root)

    return {
        "ok": True,
        "root": str(root),
        "entries": entries,
        "truncated": len(entries) >= max_files,
        "manifests": manifests,
        "total_entries": len(entries),
    }
