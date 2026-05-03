from __future__ import annotations

"""Compatibility re-exports for file and shell tools.

This module provides the public API surface that external code and tests
import from ``src.tools.file_tools``. Implementations were extracted into
``src.tools._file_io``, ``src.tools._edit_tools``, and ``src.tools._bash_exec``;
this file re-exports the canonical symbols so callers don't need to follow the
refactor.
"""

import logging  # noqa: E402
from pathlib import Path  # noqa: F401, E402

from src.tools._path_utils import safe_resolve  # noqa: F401, E402

# Eagerly import list_files (list_dir) so that auto-discovery via dir(module)
# can find it.  Previously this was fully lazy (via __getattr__), which meant
# build_registry().discover("src.tools.file_tools") would not register list_files
# because dir(file_tools) omits lazy attributes.  The lazy path still serves as
# a cache for subsequent attribute accesses.
from src.tools._file_io import list_dir  # noqa: F401, E402


# Lazy import map for re-exports. We intentionally avoid importing the
# authoritative implementation modules at import time to prevent import-time
# side-effects and cycles; attributes are imported on first access via
# module-level __getattr__ below.
_IMPORT_MAP = {
    # file I/O
    "write_file": ("src.tools._file_io", "write_file"),
    "read_file": ("src.tools._file_io", "read_file"),
    "read_file_chunk": ("src.tools._file_io", "read_file_chunk"),
    "delete_file": ("src.tools._file_io", "delete_file"),
    "rename_file": ("src.tools._file_io", "rename_file"),
    "glob": ("src.tools._file_io", "glob"),
    "list_files": ("src.tools._file_io", "list_dir"),
    # edit tools
    "edit_file": ("src.tools._edit_tools", "edit_file"),
    "edit_file_atomic": ("src.tools._edit_tools", "edit_file_atomic"),
    "edit_by_line_range": ("src.tools._edit_tools", "edit_by_line_range"),
    "multiedit": ("src.tools._edit_tools", "multiedit"),
    # diff preview gate
    "register_preview_gate": ("src.tools._diff_gate", "register_preview_gate"),
    "resolve_preview_gate": ("src.tools._diff_gate", "resolve_preview_gate"),
    # bash exec helpers and constants
    "bash": ("src.tools._bash_exec", "bash"),
    "bash_readonly": ("src.tools._bash_exec", "bash_readonly"),
    "check_background_task": ("src.tools._bash_exec", "check_background_task"),
    "_BASH_STDOUT_MAX": ("src.tools._bash_exec", "_BASH_STDOUT_MAX"),
    "_BASH_STDERR_MAX": ("src.tools._bash_exec", "_BASH_STDERR_MAX"),
    "_BASH_STDOUT_MAX_TOKENS": ("src.tools._bash_exec", "_BASH_STDOUT_MAX_TOKENS"),
    "_BASH_STDERR_MAX_TOKENS": ("src.tools._bash_exec", "_BASH_STDERR_MAX_TOKENS"),
}


def _safe_resolve(path: str, workdir: "Path | None" = None) -> Path:
    """Backward-compatible wrapper around the shared safe_resolve utility.

    Resolve *path* against *workdir*; when *workdir* is None the current
    working directory is resolved at call time.
    """
    resolved = workdir if workdir is not None else Path.cwd()
    return safe_resolve(path, resolved)


_logger = logging.getLogger(__name__)


# WorkspaceGuard: import from shared location
try:
    from src.tools._workspace_guard import WorkspaceGuard  # type: ignore[assignment]  # noqa: F401
except ImportError:
    pass


# Public re-exports are provided lazily via module-level __getattr__ below.
__all__ = list(_IMPORT_MAP.keys()) + ["_safe_resolve", "WorkspaceGuard"]


def __getattr__(name: str):
    """Lazily import and return re-exported attributes on first access.

    This avoids importing implementation modules at module import time, which
    can create import cycles or expensive side effects. Once imported the
    attribute is cached in the module globals for subsequent access.
    """
    if name in _IMPORT_MAP:
        mod_name, attr_name = _IMPORT_MAP[name]
        try:
            mod = __import__(mod_name, fromlist=[attr_name])
            val = getattr(mod, attr_name)
        except Exception as exc:  # re-raise as AttributeError for import-time lookup
            raise AttributeError(f"failed to import {name} from {mod_name}: {exc!r}")
        globals()[name] = val
        return val
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_IMPORT_MAP.keys()))
