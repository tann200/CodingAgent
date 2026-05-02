"""event_persistence.py — On-disk persistence for delivery-critical events.

TASK-REL-3: Implements the file-based ACK pattern so events that have been
published but not yet delivered to a client survive a server crash.

Design
------
* ``persist_event(event_name, payload)`` writes the event to a JSON file under
  the pending directory using an atomic ``tempfile + os.replace`` so partial
  writes never appear.  Returns the path to the file.
* ``ack_event(path)`` deletes the file after confirmed delivery (idempotent).
* ``recover_pending()`` scans the directory on startup and returns all
  unACKed events tagged with ``_recovered=True`` and ``_recovery_path``.

Only event types in ``PERSISTENT_EVENT_TYPES`` are persisted by the
``PersistentEventBus`` subclass.  Callers can also call ``persist_event``
directly for other event types they deem critical.

Backward compatibility
----------------------
The original ``PersistentEventBus`` class interface is preserved so existing
code that imports it continues to work.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.orchestration.event_bus import EventBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Event types that must survive a server crash.
PERSISTENT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent.response",
        "agent.error",
        "tool.result",
        "agent.final_response",
        "agent.stream_chunk",
    }
)

#: Maximum age (seconds) of pending files before they are pruned on recovery.
DEFAULT_MAX_AGE_SECONDS: float = 86_400.0  # 24 hours

# Allow tests to override without affecting cwd.
_pending_dir_override: Optional[Path] = None


def _get_pending_dir() -> Path:
    if _pending_dir_override is not None:
        return _pending_dir_override
    try:
        # Prefer the per-workspace agent-context path when available.
        # This honours the configured context-dir name and existing legacy
        # directories via the central tools_config helper.
        from src.tools.tools_config import agent_context_path  # type: ignore[import]

        agent_ctx = agent_context_path(Path.cwd())
        return agent_ctx / "events" / "pending"
    except Exception:
        try:
            # Fall back to the older helper if tools_config is not importable.
            from src.core.paths import get_agent_context_dir  # type: ignore[import]

            return get_agent_context_dir() / "event_pending"
        except Exception:
            # Final legacy fallback.
            return Path(".codingAgent") / "events" / "pending"


def set_pending_dir(path: Path) -> None:
    """Override the pending directory (for tests)."""
    global _pending_dir_override
    _pending_dir_override = path


# ---------------------------------------------------------------------------
# Standalone helpers (can be used without PersistentEventBus)
# ---------------------------------------------------------------------------


def persist_event(event_name: str, payload: Dict[str, Any]) -> Path:
    """Write an event to the pending directory and return its path.

    The write is atomic (temp-file + ``os.replace``).

    Parameters
    ----------
    event_name:
        EventBus topic string.
    payload:
        Event payload dict.  Keys starting with ``_`` are stripped before
        writing to avoid embedding stale metadata.

    Returns
    -------
    Path
        Path of the newly created pending file.
    """
    pending_dir = _get_pending_dir()
    pending_dir.mkdir(parents=True, exist_ok=True)

    ts_ms = int(time.time() * 1000)
    corr = str(payload.get("correlation_id", ""))[:16] or "evt"
    # Sanitize for filename safety
    corr = "".join(c if c.isalnum() or c in "-_" else "_" for c in corr)
    dest = pending_dir / f"{ts_ms}_{corr}.json"

    # Strip transient metadata keys before persisting.
    clean_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    data = {
        "event_name": event_name,
        "payload": clean_payload,
        "timestamp": time.time(),
    }

    # Prefer the centralized atomic JSON writer; fall back to the legacy
    # mkstemp+os.replace path on import/write errors.
    try:
        from src.core.io_utils import atomic_write_json

        ok = atomic_write_json(dest, data, logger=logger)
        if not ok:
            # Fall back to legacy path when the helper reports failure
            raise RuntimeError("atomic_write_json failed")
    except Exception:
        fd, tmp_path = tempfile.mkstemp(dir=str(pending_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, default=str)
            os.replace(tmp_path, str(dest))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    logger.debug("event_persistence: persisted %s → %s", event_name, dest.name)
    return dest


def ack_event(path: Path) -> None:
    """Acknowledge delivery by deleting the pending file (idempotent).

    Parameters
    ----------
    path:
        Path returned by ``persist_event``, or the string value of
        ``payload["_recovery_path"]`` cast back to ``Path``.
    """
    try:
        path.unlink(missing_ok=True)
        logger.debug("event_persistence: acked %s", path.name)
    except Exception as exc:
        logger.warning("event_persistence: failed to ack %s: %s", path, exc)


def recover_pending(
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> List[Dict[str, Any]]:
    """Return unACKed events from the pending directory, oldest first.

    Each returned dict is the original payload with two extra keys:

    * ``_recovered`` — always ``True``
    * ``_recovery_path`` — string path; pass ``Path(x["_recovery_path"])``
      to ``ack_event`` after successful redelivery.

    Stale files (older than *max_age_seconds*) are silently pruned.
    Corrupt files are discarded with a warning.
    """
    pending_dir = _get_pending_dir()
    if not pending_dir.exists():
        return []

    recovered: List[Dict[str, Any]] = []
    now = time.time()

    for p in sorted(pending_dir.glob("*.json")):
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue

        if age > max_age_seconds:
            logger.debug("event_persistence: pruning stale %s (age %.0fs)", p.name, age)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "event_persistence: corrupt pending file %s (%s) — discarding",
                p.name,
                exc,
            )
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        # Flatten into a single dict matching what publish() receives.
        event_payload = raw.get("payload", raw)
        event_payload["_recovered"] = True
        event_payload["_recovery_path"] = str(p)
        event_payload.setdefault("event", raw.get("event_name", "unknown"))
        recovered.append(event_payload)

    if recovered:
        logger.info("event_persistence: recovered %d unACKed event(s)", len(recovered))
    return recovered


# ---------------------------------------------------------------------------
# PersistentEventBus subclass (backward-compatible class interface)
# ---------------------------------------------------------------------------


class PersistentEventBus(EventBus):
    """EventBus subclass that persists delivery-critical events to disk.

    On startup, call ``initialize()`` to replay any events that were not
    acknowledged before the previous shutdown.

    TASK-REL-3 improvements over the original implementation:
    * Atomic writes via ``os.replace`` (was ``shutil.move``).
    * ``PERSISTENT_EVENT_TYPES`` filter — only agent/tool response events
      are persisted; high-frequency events (heartbeats, logs) are not.
    * Path-based ACK — no fragile timestamp/session_id glob search.
    * Stale file pruning (default 24 h).
    * ``_recovered=True`` tag on replayed events for dedup guards.
    """

    def __init__(self, pending_dir: Optional[Path] = None) -> None:
        super().__init__()
        if pending_dir is not None:
            set_pending_dir(pending_dir)
        self._initialized = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _persist_event(self, event_name: str, payload: Any) -> Optional[Path]:
        """Persist *event_name* + *payload* if it is in PERSISTENT_EVENT_TYPES."""
        if event_name not in PERSISTENT_EVENT_TYPES:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return persist_event(event_name, payload)
        except Exception as exc:
            logger.warning(
                "PersistentEventBus: failed to persist %s: %s", event_name, exc
            )
            return None

    def _ack_event(self, payload: Any) -> bool:
        """Remove the persisted file for *payload* (path-based)."""
        if not isinstance(payload, dict):
            return False
        path_str = payload.get("_persistence_path") or payload.get("_recovery_path")
        if not path_str:
            return False
        ack_event(Path(path_str))
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recover_pending(self, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> int:
        """Replay unACKed events and return the count."""
        events = recover_pending(max_age_seconds=max_age_seconds)
        for ev in events:
            event_name = ev.get("event", "unknown")
            try:
                super().publish(event_name, ev)
            except Exception as exc:
                logger.warning(
                    "PersistentEventBus: failed to re-publish %s: %s", event_name, exc
                )
        return len(events)

    def initialize(self) -> int:
        """Initialize and recover pending events.  Call once at startup."""
        if self._initialized:
            return 0
        _get_pending_dir().mkdir(parents=True, exist_ok=True)
        recovered = self.recover_pending()
        self._initialized = True
        return recovered


# ---------------------------------------------------------------------------
# Module-level singleton (backward compat)
# ---------------------------------------------------------------------------

_persistent_bus: Optional[PersistentEventBus] = None


def get_persistent_event_bus() -> PersistentEventBus:
    """Return (creating if needed) the module-level PersistentEventBus singleton."""
    global _persistent_bus
    if _persistent_bus is None:
        _persistent_bus = PersistentEventBus()
    return _persistent_bus
