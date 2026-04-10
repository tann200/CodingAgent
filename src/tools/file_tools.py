from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Any, Optional

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
from src.tools._tool import tool
from src.tools._security import (
    DANGEROUS_PATTERNS,
    SAFE_COMMANDS,
    TEST_COMPILE_COMMANDS,
    RESTRICTED_COMMANDS,
    RESTRICTED_ALLOWED_SUBCOMMANDS,
    CODE_EXEC_INTERPRETERS,
    CODE_EXEC_FLAGS,
    TAR_EXTRACT_FLAGS,
    TAR_CREATE_FLAGS,
    GIT_SAFE_SUBCOMMANDS,
    SED_WRITE_FLAGS,
)


# ── Diff preview gate — implementation lives in _diff_gate.py ─────────────────
# Re-exported here so that:
#   • patch("src.tools.file_tools.resolve_preview_gate") still works (tests + preview_coordinator)
#   • from src.tools.file_tools import resolve_preview_gate still works (preview_coordinator)
from src.tools._diff_gate import (
    _pending_previews,
    _preview_rejected,
    _preview_gate_lock,
    register_preview_gate,
    resolve_preview_gate,
    _publish_diff_preview,
)


# Default working directory.  External projects should call
# ``tools_config.configure(default_workdir=Path("/my/project"))`` at startup
# rather than relying on this module-level constant.
DEFAULT_WORKDIR = Path.cwd()

# ── File I/O constants — authoritative values live in _file_io.py ──────────────
from src.tools._file_io import (
    _READ_FILE_MAX_CHARS,
    _READ_FILE_MAX_LINE,
    _WRITE_HARD_LINE_LIMIT,
    _WRITE_WARN_LINE_LIMIT,
)

# ── Bash execution — implementation lives in _bash_exec.py ────────────────────
# Constants re-exported here because tests import them directly from file_tools.
from src.tools._bash_exec import (
    bash,
    bash_readonly,
    check_background_task,
    _truncate_bash_output,
    _check_shell_flags,
    _BASH_STDOUT_MAX,
    _BASH_STDOUT_MAX_TOKENS,
    _BASH_STDERR_MAX,
    _BASH_STDERR_MAX_TOKENS,
)


def _safe_resolve(path: str, workdir: Path = DEFAULT_WORKDIR) -> Path:
    """Backward-compatible wrapper around the shared safe_resolve utility (#29)."""
    return safe_resolve(path, workdir)


# ── File I/O tools — implementation lives in _file_io.py ─────────────────────
from src.tools._file_io import (
    write_file,
    read_file,
    list_dir,
    delete_file,
    rename_file,
    sandbox_info,
    read_file_chunk,
    glob,
    tail_log_file,
    create_directory,
    read_file_bytes,
    _OS_JUNK,
)

# ── Edit tools — implementation lives in _edit_tools.py ───────────────────────
from src.tools._edit_tools import (
    _fuzzy_find,
    edit_file,
    edit_by_line_range,
    edit_file_atomic,
    multiedit,
    _EDIT_NET_CHANGE_WARN,
)
