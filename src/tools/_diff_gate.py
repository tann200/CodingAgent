"""Diff preview gate — threading state and publish/resolve helpers.

Extracted from file_tools.py so that bash_exec, file_io, and edit_tools can
all import gate primitives without depending on the full file_tools module.

Public names are re-exported from src.tools.file_tools for backward
compatibility; patch targets at ``src.tools.file_tools.resolve_preview_gate``
continue to work because file_tools carries a named re-export binding.
"""

from __future__ import annotations

import logging
import threading

_logger = logging.getLogger(__name__)


# ── TUI-05: blocking diff preview gate ────────────────────────────────────────
# edit_file_atomic() registers a threading.Event here before publishing the
# file.diff.preview event, then waits for it.  The orchestrator's
# preview.confirmed / preview.rejected handlers call resolve_preview_gate().
_pending_previews: dict[str, threading.Event] = {}
_preview_rejected: set[str] = set()
_preview_gate_lock: threading.Lock = threading.Lock()


def register_preview_gate(path_key: str) -> threading.Event:
    """Register a pending diff-preview approval; return the Event to wait on."""
    ev = threading.Event()
    with _preview_gate_lock:
        _pending_previews[path_key] = ev
    return ev


def resolve_preview_gate(path_key: str, approved: bool) -> None:
    """Resolve a pending diff preview gate.  Called from EventBus handler."""
    with _preview_gate_lock:
        if not approved:
            _preview_rejected.add(path_key)
        ev = _pending_previews.pop(path_key, None)
    if ev is not None:
        ev.set()


def _publish_diff_preview(path: str, diff: str, is_new_file: bool = False) -> None:
    """M4: Publish a diff preview event before a file write is applied.

    Subscribers (e.g. TUI) receive this to show the user what is about
    to change, giving them a chance to see (and in future, reject) edits.
    """
    try:
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()
        bus.publish(
            "file.diff.preview",
            {
                "path": path,
                "diff": diff,
                "is_new_file": is_new_file,
            },
        )
    except Exception as _exc:
        _logger.debug(
            "_publish_diff_preview: event bus unavailable (non-fatal): %s", _exc
        )
        pass  # Never block the write if event bus is unavailable
