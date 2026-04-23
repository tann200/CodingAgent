"""Diff preview gate — threading state and publish/resolve helpers.

Extracted from file_tools.py so that bash_exec, file_io, and edit_tools can
all import gate primitives without depending on the full file_tools module.

Public names are re-exported from src.tools.file_tools for backward
compatibility; patch targets at ``src.tools.file_tools.resolve_preview_gate``
continue to work because file_tools carries a named re-export binding.
"""

from __future__ import annotations

import logging
import os
import threading
import time

_logger = logging.getLogger(__name__)


# ── TUI-05: blocking diff preview gate ────────────────────────────────────────
# edit_file_atomic() registers a threading.Event here before publishing the
# file.diff.preview event, then waits for it.  The orchestrator's
# preview.confirmed / preview.rejected handlers call resolve_preview_gate().
_pending_previews: dict[str, threading.Event] = {}
_preview_rejected: set[str] = set()
_preview_gate_lock: threading.Lock = threading.Lock()
# When a resolver runs before a registrant, record the resolved outcome here
# along with a monotonic timestamp so stale pre-resolutions can be purged.
# Mapping: path_key -> (approved: bool, ts: float)
_preview_result: dict[str, tuple[bool, float]] = {}

# TTL for entries in _preview_result (seconds). Default 5 minutes.
# Can be overridden via the environment variable
# CODINGAGENT_PREVIEW_RESULT_TTL (float seconds) for deployments/tests.
_PREVIEW_RESULT_TTL: float = 300.0
try:
    _PREVIEW_RESULT_TTL = float(
        os.environ.get("CODINGAGENT_PREVIEW_RESULT_TTL", str(_PREVIEW_RESULT_TTL))
    )
except Exception:
    # Keep default if env var is invalid
    pass


def reset_preview_gate() -> None:
    """Reset all preview-gate internal state. Useful for tests.

    Clears pending previews, recorded rejections, and any pre-resolved results.
    This acquires the internal lock.
    """
    with _preview_gate_lock:
        _pending_previews.clear()
        _preview_rejected.clear()
        _preview_result.clear()


def pop_preview_rejection(path_key: str) -> bool:
    """Atomically check for a rejection and remove it.

    Returns True if the path_key was recorded as rejected (and removes it),
    False otherwise.
    """
    with _preview_gate_lock:
        if path_key in _preview_rejected:
            _preview_rejected.discard(path_key)
            return True
        return False


def has_pending_previews() -> bool:
    """Return True if any preview gates are currently pending."""
    with _preview_gate_lock:
        return bool(_pending_previews)


def get_preview_rejected_count() -> int:
    """Return the number of preview rejections currently recorded."""
    with _preview_gate_lock:
        return len(_preview_rejected)


def is_preview_rejected(path_key: str) -> bool:
    """Return True if the given path_key is recorded as rejected."""
    with _preview_gate_lock:
        return path_key in _preview_rejected


def set_preview_result_ttl(seconds: float) -> None:
    """Set the TTL (in seconds) for pre-resolved preview results.

    This is primarily provided for tests so they can exercise expiry without
    waiting for the full default TTL.
    """
    global _PREVIEW_RESULT_TTL
    try:
        _PREVIEW_RESULT_TTL = float(seconds)
    except Exception:
        # Ignore invalid inputs — keep existing TTL
        pass


def get_preview_result_ttl() -> float:
    """Return the current TTL used for pre-resolved results."""
    return _PREVIEW_RESULT_TTL


def _purge_expired_preview_results() -> None:
    """Remove entries from _preview_result that are older than the TTL.

    Must be called while holding _preview_gate_lock.
    """
    if _PREVIEW_RESULT_TTL <= 0:
        # TTL of 0 or negative means entries expire immediately; purge all
        # recorded pre-resolved results and any recorded rejections that were
        # associated with those pre-resolved results.  We only remove
        # rejection entries that correspond to pre-resolved results so that
        # immediate rejections recorded for present registrants are not
        # accidentally cleared.
        expired_keys = list(_preview_result.keys())
        _preview_result.clear()
        for k in expired_keys:
            _preview_rejected.discard(k)
        return
    now = time.monotonic()
    expired = [
        k
        for k, (_approved, ts) in _preview_result.items()
        if now - ts >= _PREVIEW_RESULT_TTL
    ]
    for k in expired:
        _preview_result.pop(k, None)
        # Also remove any recorded pre-resolve rejection for this key.
        _preview_rejected.discard(k)


def register_preview_gate(path_key: str) -> threading.Event:
    """Register a pending diff-preview approval; return the Event to wait on."""
    ev = threading.Event()
    with _preview_gate_lock:
        # Purge any expired pre-resolved results before checking.
        _purge_expired_preview_results()

        # If a resolution already arrived before we registered, honour it
        # immediately and return an Event that's already set.
        if path_key in _preview_result:
            approved, _ts = _preview_result.pop(path_key)
            # Keep rejected semantics in _preview_rejected for compatibility
            if not approved:
                _preview_rejected.add(path_key)
            ev.set()
        else:
            _pending_previews[path_key] = ev
    return ev


def resolve_preview_gate(path_key: str, approved: bool) -> None:
    """Resolve a pending diff preview gate.  Called from EventBus handler."""
    with _preview_gate_lock:
        # Purge expired pre-resolved results first so we don't accumulate
        # stale entries.  This must be done while holding the lock.
        _purge_expired_preview_results()

        ev = _pending_previews.pop(path_key, None)
        if ev is None:
            # No registrant yet — record the result so a future registrant
            # can observe it immediately instead of waiting.  Store a tuple
            # of (approved, timestamp) so entries can expire after TTL.
            _preview_result[path_key] = (approved, time.monotonic())
            if not approved:
                _preview_rejected.add(path_key)
        else:
            # Registrant was present — apply rejection semantics and signal
            if not approved:
                _preview_rejected.add(path_key)
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
