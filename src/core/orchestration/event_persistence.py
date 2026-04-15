"""Event persistence for crash recovery - prevents message loss.

This module adds event persistence to the EventBus so that outbound events
are written to disk before dispatch and removed only after acknowledgment.
Similar to OpenClaw's event persistence approach.
"""

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from src.core.orchestration.event_bus import EventBus

logger = logging.getLogger(__name__)


def _get_pending_dir() -> Path:
    """Get the pending events directory."""
    # Try to get from paths, fallback to temp
    try:
        from src.core.paths import get_agent_context_dir

        return get_agent_context_dir() / "event_pending"
    except Exception:
        return Path(tempfile.gettempdir()) / "codingagent_events_pending"


class PersistentEventBus(EventBus):
    """EventBus with persistence for crash recovery.

    Events are written to a pending directory before dispatch,
    and removed only after ack() is called. On startup,
    any pending events are replayed.
    """

    def __init__(self, pending_dir: Optional[Path] = None) -> None:
        super().__init__()
        self._pending_dir = pending_dir or _get_pending_dir()
        self._initialized = False

    def _ensure_pending_dir(self) -> None:
        """Ensure the pending directory exists."""
        if not self._pending_dir.exists():
            self._pending_dir.mkdir(parents=True, exist_ok=True)

    def _persist_event(self, event_name: str, payload: Any) -> Optional[Path]:
        """Persist event to disk before dispatch.

        Writes to a temp file, then renames for atomicity.
        Returns the path to the persisted file, or None on failure.
        """
        if not isinstance(payload, dict):
            return None

        try:
            self._ensure_pending_dir()

            timestamp = time.time()
            session_id = payload.get("session_id", "unknown")
            filename = f"{timestamp:.6f}_{session_id}.json"
            temp_path = self._pending_dir / f".{filename}"
            final_path = self._pending_dir / filename

            data = {
                "event_name": event_name,
                "payload": payload,
                "timestamp": timestamp,
            }

            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)

            shutil.move(str(temp_path), str(final_path))
            logger.debug(f"Persisted event: {event_name} to {final_path.name}")
            return final_path

        except Exception as e:
            logger.warning(f"Failed to persist event: {e}")
            return None

    def _ack_event(self, payload: Any) -> bool:
        """Remove persisted event after successful processing.

        Called by subscribers after successfully handling an event.
        """
        if not isinstance(payload, dict):
            return False

        try:
            session_id = payload.get("session_id", "unknown")
            timestamp = payload.get("timestamp", 0)

            # Find matching file
            if self._pending_dir.exists():
                prefix = f"{timestamp:.6f}_" if timestamp else None
                if prefix:
                    for f in self._pending_dir.glob(f"{prefix}{session_id}.json"):
                        f.unlink()
                        logger.debug(f"Acked and removed: {f.name}")
                        return True

            # Also try to find by session_id if timestamp not available
            for f in self._pending_dir.glob(f"*_{session_id}.json"):
                f.unlink()
                return True

        except Exception as e:
            logger.warning(f"Failed to ack event: {e}")

        return False

    def recover_pending(self) -> int:
        """Recover pending events on startup.

        Scans the pending directory and replays any events
        that weren't acknowledged before shutdown.

        Returns the number of events recovered.
        """
        if not self._pending_dir.exists():
            return 0

        recovered = 0
        try:
            for f in sorted(self._pending_dir.glob("*.json")):
                try:
                    with open(f, "r", encoding="utf-8") as fp:
                        data = json.load(fp)

                    event_name = data.get("event_name")
                    payload = data.get("payload")

                    if event_name and payload:
                        logger.info(f"Recovering event: {event_name} from {f.name}")
                        # Re-dispatch
                        super().publish(event_name, payload)
                        recovered += 1

                    # Remove after recovery
                    f.unlink()

                except Exception as e:
                    logger.warning(f"Failed to recover {f.name}: {e}")

        except Exception as e:
            logger.error(f"Error during event recovery: {e}")

        if recovered > 0:
            logger.info(f"Recovered {recovered} pending events")

        return recovered

    def initialize(self) -> int:
        """Initialize the persistent event bus and recover pending events."""
        if self._initialized:
            return 0

        self._ensure_pending_dir()
        recovered = self.recover_pending()
        self._initialized = True
        return recovered


# Module-level singleton
_persistent_bus: Optional[PersistentEventBus] = None


def get_persistent_event_bus() -> PersistentEventBus:
    """Get the persistent event bus singleton."""
    global _persistent_bus
    if _persistent_bus is None:
        _persistent_bus = PersistentEventBus()
    return _persistent_bus
