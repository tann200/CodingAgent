"""_lint_verify.py — shared pre-write lint verification helper.

Provides a single implementation used by both _file_io.py (write_file) and
_edit_tools.py (edit_file, edit_file_atomic, edit_by_line_range, multiedit).

This avoids duplicating the inner _norm_lint_key function and the
TemporaryDirectory-based baseline-comparison pattern in both modules.
"""

from __future__ import annotations

import logging
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_PREVALIDATED_CACHE: dict[tuple[str, int, int], float] = {}
_PREVALIDATED_LOCK = threading.Lock()
_CACHE_TTL = 10.0  # seconds


def mark_content_prevalidated(p: Path, content: str) -> None:
    """Record that candidate content for file p has passed sandbox AST validation."""
    key = (str(p.resolve()), len(content), hash(content))
    now = time.monotonic()
    with _PREVALIDATED_LOCK:
        _PREVALIDATED_CACHE[key] = now
        if len(_PREVALIDATED_CACHE) > 256:
            for k, t in list(_PREVALIDATED_CACHE.items()):
                if now - t > _CACHE_TTL:
                    _PREVALIDATED_CACHE.pop(k, None)


def is_content_prevalidated(p: Path, content: str) -> bool:
    """Return True if candidate content was already validated recently."""
    key = (str(p.resolve()), len(content), hash(content))
    now = time.monotonic()
    with _PREVALIDATED_LOCK:
        ts = _PREVALIDATED_CACHE.get(key)
        if ts is not None and (now - ts) <= _CACHE_TTL:
            return True
    return False


def _norm_lint_key(err: dict[str, Any], path_tokens: list[str]) -> tuple[Any, Any, str]:
    """Normalise a lint error dict to a stable key for baseline comparison."""
    msg = str(err.get("message") or "")
    for token in path_tokens:
        if token:
            msg = msg.replace(token, "<path>")
    msg = re.sub(r"\((?:<path>|[^()]+), line (\d+)\)", r"(<path>, line \1)", msg)
    return (err.get("line"), err.get("code"), msg)


def verify_candidate_content(
    p: Path,
    content: str,
    workdir: Path,
    op: str = "write",
) -> dict[str, Any] | None:
    """Validate *content* as a candidate replacement for file *p*.

    Runs a fast lint check on the current file (baseline) and on *content*
    written to a temporary sibling file.  Returns an error dict if the
    candidate introduces new lint errors; ``None`` on success or when lint
    is unavailable.

    Parameters
    ----------
    p:
        Absolute path of the target file (may not exist yet for new files).
    content:
        Proposed new content.
    workdir:
        Project working directory, forwarded to the linter.
    op:
        Operation label used in the error message (``"write"`` or ``"edit"``).
    """
    try:
        from src.tools.lint_dispatch import quick_lint

        # Fast path: if content was already pre-validated by tool pipeline sandbox (SPEC-P3.5)
        # and file is new or has no baseline errors, avoid redundant tempdir & subprocess invocation.
        prevalidated = is_content_prevalidated(p, content)
        if prevalidated and not p.is_file():
            return None

        baseline_result = quick_lint(str(p), workdir)
        baseline_errors = list((baseline_result or {}).get("lint_errors") or [])
        if prevalidated and not baseline_errors:
            return None

        baseline_keys: set[tuple[Any, Any, str]] = set()
        for err in baseline_errors:
            if isinstance(err, dict):
                baseline_keys.add(_norm_lint_key(err, [str(p), p.name]))

        with tempfile.TemporaryDirectory(
            dir=str(p.parent),
            prefix=f".{p.stem}.",
        ) as tmp_dir:
            tmp_file = Path(tmp_dir) / p.name
            tmp_file.write_text(content, encoding="utf-8")

            lint_result = quick_lint(str(tmp_file), workdir)
            if lint_result and lint_result.get("lint_errors"):
                new_errors = [
                    err
                    for err in lint_result["lint_errors"]
                    if isinstance(err, dict)
                    and _norm_lint_key(
                        err, [str(tmp_file), tmp_file.name, str(p), p.name]
                    )
                    not in baseline_keys
                ]
            else:
                new_errors = []

        if new_errors:
            errors_str = "\n".join(
                f"Line {e.get('line', '?')}: {e.get('message')}"
                for e in new_errors[:5]
            )
            return {
                "path": str(p),
                "status": "error",
                "error": (
                    f"Pre-write verification failed. {op.capitalize()} introduces"
                    f" syntax errors:\n\n{errors_str}"
                ),
                "lint_errors": new_errors,
            }
    except Exception as exc:
        _logger.warning("pre-write verification error (%s %s): %s", op, p, exc)
    return None
