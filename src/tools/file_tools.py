from __future__ import annotations

"""Compatibility re-exports for file and shell tools.

This module provides the public API surface that external code and tests
import from ``src.tools.file_tools``. Implementations were extracted into
``src.tools._file_io``, ``src.tools._edit_tools``, and ``src.tools._bash_exec``;
this file re-exports the canonical symbols so callers don't need to follow the
refactor.
"""

# ruff: noqa: E402
import logging
from pathlib import Path  # noqa: F401
from typing import Dict

from src.tools._path_utils import safe_resolve  # noqa: F401

# Implementation modules (authoritative implementations live here)
from src.tools._file_io import (
    write_file as _write_file,
    read_file as _read_file,
    read_file_chunk as _read_file_chunk,
    delete_file as _delete_file,
    rename_file as _rename_file,
    glob as _glob,
    list_dir as _list_dir,
)
from src.tools._edit_tools import (
    edit_file as _edit_file,
    edit_file_atomic as _edit_file_atomic,
    edit_by_line_range as _edit_by_line_range,
    multiedit as _multiedit,
)
from src.tools._diff_gate import (
    register_preview_gate as _register_preview_gate,
    resolve_preview_gate as _resolve_preview_gate,
)
from src.tools._bash_exec import (
    bash as _bash,
    bash_readonly as _bash_readonly,
    check_background_task as _check_background_task,
    _BASH_STDOUT_MAX as _BASH_STDOUT_MAX,
    _BASH_STDERR_MAX as _BASH_STDERR_MAX,
    _BASH_STDOUT_MAX_TOKENS as _BASH_STDOUT_MAX_TOKENS,
    _BASH_STDERR_MAX_TOKENS as _BASH_STDERR_MAX_TOKENS,
)


def _safe_resolve(path: str, workdir: "Path | None" = None) -> Path:
    """Backward-compatible wrapper around the shared safe_resolve utility.

    Resolve *path* against *workdir*; when *workdir* is None the current
    working directory is resolved at call time.
    """
    resolved = workdir if workdir is not None else Path.cwd()
    return safe_resolve(path, resolved)


_logger = logging.getLogger(__name__)


class WorkspaceGuard:
    """No-op guard when src.core is not available."""

    def guard_operation(self, *args: object, **kwargs: object) -> Dict[str, str]:
        return {"status": "ok"}


try:
    from src.core.orchestration.workspace_guard import WorkspaceGuard  # type: ignore[assignment]  # noqa: F401
except ImportError:
    pass


# Public re-exports (kept as simple bindings so tests can import them from
# src.tools.file_tools without depending on the original implementation file).
write_file = _write_file
read_file = _read_file
read_file_chunk = _read_file_chunk
delete_file = _delete_file
rename_file = _rename_file
glob = _glob
list_files = _list_dir

edit_file = _edit_file
edit_file_atomic = _edit_file_atomic
edit_by_line_range = _edit_by_line_range
multiedit = _multiedit

# Bash execution re-exports (moved to _bash_exec.py)
bash = _bash
bash_readonly = _bash_readonly
check_background_task = _check_background_task

# Diff preview gate re-exports (kept for backward compatibility)
register_preview_gate = _register_preview_gate
resolve_preview_gate = _resolve_preview_gate

# Expose authoritative truncation constants used by tests
_BASH_STDOUT_MAX = _BASH_STDOUT_MAX
_BASH_STDERR_MAX = _BASH_STDERR_MAX
_BASH_STDOUT_MAX_TOKENS = _BASH_STDOUT_MAX_TOKENS
_BASH_STDERR_MAX_TOKENS = _BASH_STDERR_MAX_TOKENS
