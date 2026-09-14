"""tool_constants.py — Shared tool-classification sets and permission audit helper.

These constants are used by ``orchestrator.py``, ``permission_gateway.py``, and
``loop_guards.py``.  Keeping them in a leaf module (no internal imports) eliminates
the circular dependency where ``permission_gateway`` previously had to import back
from ``orchestrator``.

The canonical tool-classification sets live in ``src.tools.constants`` (see
PHASE-4 item 4.6); this module re-exports them so existing importers keep
working, and retains the audit-log helper that lives at the orchestration
layer.

Phase A of the orchestrator refactoring plan.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from src.tools.constants import (  # noqa: F401
    DRY_RUN_BLOCKED_TOOLS,
    MODIFYING_TOOLS,
    PERMISSION_REQUIRED_TOOLS,
    PERM_ORDER,
    WRITE_TOOLS_REQUIRING_READ,
)

logger = logging.getLogger(__name__)


MAX_AUDIT_FILE_SIZE: int = 1_048_576  # 1 MiB — rotate when exceeded
_MAX_AUDIT_KEEP_LINES: int = 1_000  # keep at most this many entries after rotation


def _write_permission_audit(
    working_dir: Any,
    tool_name: str,
    args: dict,
    decision: str,
    reason: str = "",
) -> None:
    """PERM-W5: Append a permission audit entry to .agent/permission_audit.jsonl.

    Each line is a JSON object with fields: timestamp, tool, decision, reason.
    The file is rotated when it exceeds MAX_AUDIT_FILE_SIZE (1 MiB) to prevent
    unbounded growth across sessions.  Older entries beyond _MAX_AUDIT_KEEP_LINES
    are discarded on rotation.
    Args are not logged to avoid leaking sensitive values.
    """
    try:
        # Resolve working dir path
        wd = Path(str(working_dir)) if working_dir else Path.cwd()

        try:
            # Prefer centralised helper so tests and code share a single
            # canonical decision point for the audit directory name.
            from src.tools.tools_config import get_audit_dir

            audit_dir = get_audit_dir(wd)
        except Exception:
            audit_dir = wd / ".codingAgent"
            audit_dir.mkdir(parents=True, exist_ok=True)

        audit_path = audit_dir / "permission_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)

        # Rotate if the file has grown past the size limit.
        if audit_path.exists() and audit_path.stat().st_size >= MAX_AUDIT_FILE_SIZE:
            _rotate_audit_log(audit_path)

        entry = json.dumps(
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "tool": tool_name,
                "decision": decision,
                "reason": reason,
            }
        )
        with audit_path.open("a", encoding="utf-8") as _f:
            _f.write(entry + "\n")
    except Exception:
        pass  # audit failures must never block tool execution


def _rotate_audit_log(path: Path) -> None:
    """Truncate *path* to at most ``_MAX_AUDIT_KEEP_LINES`` recent entries."""
    try:
        with path.open("r", encoding="utf-8") as _f:
            lines = _f.readlines()
        if len(lines) <= _MAX_AUDIT_KEEP_LINES:
            return
        # Keep only the *last* N lines (most recent entries).
        with path.open("w", encoding="utf-8") as _f:
            _f.writelines(lines[-_MAX_AUDIT_KEEP_LINES:])
    except Exception:
        pass  # rotation failures must never block tool execution
