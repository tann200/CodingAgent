from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

_logger = logging.getLogger(__name__)

# Provide a no-op fallback so pyright sees a concrete class even when
# src.core is unavailable.  The real import shadows it at runtime.


class WorkspaceGuard:
    """No-op guard when src.core is not available."""

    def guard_operation(self, *args: object, **kwargs: object) -> Dict[str, str]:
        return {"status": "ok"}


try:
    from src.core.orchestration.workspace_guard import WorkspaceGuard  # type: ignore[assignment]
except ImportError:
    pass  # fallback class above is used


from src.tools._path_utils import safe_resolve

# Re-export selected implementations for backward compatibility
from src.tools._file_io import (
    write_file,
    read_file,
    list_dir as list_files,
    delete_file,
    rename_file,
    glob,
)
from src.tools._file_io import (
    read_file_chunk,
    tail_log_file,
    read_file_bytes,
    sandbox_info,
    create_directory,
)
from src.tools._edit_tools import (
    edit_file,
    edit_by_line_range,
    edit_file_atomic,
    multiedit,
)

# Re-export bash execution API (moved to _bash_exec.py) for backward compatibility
from src.tools._bash_exec import (
    bash,
    bash_readonly,
    check_background_task,
    _BASH_STDOUT_MAX,
    _BASH_STDERR_MAX,
    _BASH_STDOUT_MAX_TOKENS,
    _BASH_STDERR_MAX_TOKENS,
)

# Re-export diff preview gate helpers so tests and preview_coordinator can
# patch/ import resolve_preview_gate / register_preview_gate via
# src.tools.file_tools.resolve_preview_gate
from src.tools._diff_gate import register_preview_gate, resolve_preview_gate


# ── Diff preview gate — implementation lives in _diff_gate.py ─────────────────
# Re-exported here so that:
#   • patch("src.tools.file_tools.resolve_preview_gate") still works (tests + preview_coordinator)
#   • from src.tools.file_tools import resolve_preview_gate still works (preview_coordinator)


# Default working directory.  External projects should call
# ``tools_config.configure(default_workdir=Path("/my/project"))`` at startup
# rather than relying on this module-level constant.
DEFAULT_WORKDIR = Path.cwd()

# ── File I/O constants — authoritative values live in _file_io.py ──────────────

# ── Bash execution — implementation lives in _bash_exec.py ────────────────────
# Constants re-exported here because tests import them directly from file_tools.


def _safe_resolve(path: str, workdir: Path = DEFAULT_WORKDIR) -> Path:
    """Backward-compatible wrapper around the shared safe_resolve utility (#29)."""
    return safe_resolve(path, workdir)


# ── File I/O tools — implementation lives in _file_io.py ─────────────────────

# ── Edit tools — implementation lives in _edit_tools.py ───────────────────────
