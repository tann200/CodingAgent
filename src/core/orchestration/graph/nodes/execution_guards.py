"""execution_guards.py — P3-T6: Guard/validation helpers extracted from execution_helpers.

Contains:
- _validate_python_syntax: syntax check before writing .py files
- _capture_snapshot: pre-write snapshot capture for rollback
"""
from __future__ import annotations

import ast as _ast
import hashlib
import time
from pathlib import Path
from typing import Optional


def _validate_python_syntax(content: str, path_hint: str = "") -> Optional[str]:
    """Return an error string if *content* is not valid Python, else None.

    Only applies to .py files. Non-Python content always returns None.
    Called before committing a write_file result to prevent the agent from
    writing files that would immediately cause import/syntax errors.
    """
    if not path_hint.endswith(".py"):
        return None
    try:
        _ast.parse(content)
        return None
    except SyntaxError as exc:
        return (
            f"Syntax error in generated Python for '{path_hint}': "
            f"{exc.msg} (line {exc.lineno})"
        )


def _capture_snapshot(path: str, working_dir: str) -> Optional[str]:
    """Read the current content of *path* and save it to the snapshot dir.

    Must be called **before** a write_file / edit_file tool result is committed
    so that the pre-write content is preserved for rollback by debug_node.

    Returns the absolute snapshot file path on success, ``None`` on any error
    (non-fatal — snapshot loss is acceptable over crashing execution).
    """
    try:
        p = (Path(working_dir) / path).resolve()
        if not p.exists():
            return None
        snap_dir = Path(working_dir) / ".codingAgent" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        slug = hashlib.md5(str(p).encode()).hexdigest()[:8]
        snap_path = snap_dir / f"{slug}_{ts}{p.suffix}"
        snap_path.write_bytes(p.read_bytes())
        return str(snap_path)
    except Exception:
        return None
