"""Frozen snapshot memory system.

This module provides a memory system that captures a snapshot at session start
and doesn't change mid-session. This preserves prompt caching - the system
prompt stays stable throughout the session while the live file can still be
updated.

The frozen snapshot pattern:
1. At session start (or first load), capture the memory content
2. Return the frozen snapshot for system prompt injection
3. Mid-session writes update the file but NOT the snapshot
4. On next session start, a new snapshot is captured
"""

import threading
from typing import Any, Dict, List, Optional

from src.core.paths import get_memory_path


class FrozenMemoryStore:
    """Memory store that provides frozen snapshots for system prompt injection.

    Maintains two states:
    - _frozen_snapshot: captured at load time, used for system prompt
    - _live_entries: current entries from file, used for tool responses

    This ensures mid-session writes don't break prompt caching.
    """

    # Default character limits (like hermes-agent)
    DEFAULT_MEMORY_LIMIT = 2200
    DEFAULT_USER_LIMIT = 1375

    def __init__(
        self,
        memory_char_limit: int = DEFAULT_MEMORY_LIMIT,
        user_char_limit: int = DEFAULT_USER_LIMIT,
    ):
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit

        # Frozen snapshot for system prompt - set once at load time
        self._frozen_snapshot: str = ""

        # Live entries - can be updated mid-session
        self._live_entries: List[str] = []

        # Track if loaded
        self._loaded = False
        self._load_lock = threading.RLock()

    def load_from_disk(self) -> None:
        """Load memory from disk and capture frozen snapshot.

        Call this at session start. The frozen snapshot will NOT change
        mid-session even if the file is updated.
        """
        with self._load_lock:
            if self._loaded:
                return

            mem_path = get_memory_path()
            if not mem_path.exists():
                self._live_entries = []
                self._frozen_snapshot = ""
                self._loaded = True
                return

            try:
                content = mem_path.read_text(encoding="utf-8")
            except Exception:
                self._live_entries = []
                self._frozen_snapshot = ""
                self._loaded = True
                return

            # Parse entries (lines starting with "- [")
            self._live_entries = [
                ln.strip()
                for ln in content.splitlines()
                if ln.strip().startswith("- [")
            ]

            # Capture frozen snapshot for system prompt
            self._frozen_snapshot = self._render_snapshot()
            self._loaded = True

    def _render_snapshot(self) -> str:
        """Render the frozen snapshot for system prompt injection."""
        if not self._live_entries:
            return ""

        current = len("\n".join(self._live_entries))
        limit = self.memory_char_limit
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
        separator = "=" * 46

        return f"{separator}\n{header}\n{separator}\n" + "\n".join(self._live_entries)

    def get_frozen_snapshot(self) -> str:
        """Get the frozen snapshot for system prompt injection.

        Returns the state captured at load_from_disk() time, NOT the live state.
        This keeps the system prompt stable across all turns.
        """
        return self._frozen_snapshot

    def get_live_entries(self) -> List[str]:
        """Get the current live entries from memory."""
        return self._live_entries.copy()

    def get_usage(self) -> Dict[str, Any]:
        """Get current memory usage stats."""
        if not self._live_entries:
            return {
                "entry_count": 0,
                "char_count": 0,
                "char_limit": self.memory_char_limit,
                "percentage": 0,
            }

        content = "\n".join(self._live_entries)
        char_count = len(content)

        return {
            "entry_count": len(self._live_entries),
            "char_count": char_count,
            "char_limit": self.memory_char_limit,
            "percentage": min(100, int((char_count / self.memory_char_limit) * 100)),
        }

    def reload(self) -> None:
        """Reload from disk and capture new frozen snapshot.

        Use this to refresh the snapshot (e.g., at session resume).
        """
        with self._load_lock:
            self._loaded = False
            self.load_from_disk()


# Global singleton instance
_memory_store: Optional[FrozenMemoryStore] = None
_memory_store_lock = threading.Lock()


def get_memory_store() -> FrozenMemoryStore:
    """Get the global memory store singleton."""
    global _memory_store
    with _memory_store_lock:
        if _memory_store is None:
            _memory_store = FrozenMemoryStore()
        return _memory_store


def load_memory_snapshot() -> str:
    """Load memory from disk and return frozen snapshot for system prompt.

    This should be called at session start.
    """
    store = get_memory_store()
    store.load_from_disk()
    return store.get_frozen_snapshot()


def get_memory_for_prompt() -> str:
    """Get the frozen memory snapshot for system prompt injection.

    Loads from disk on first call so the snapshot is populated without an
    explicit ``load_memory_snapshot()`` call at session start.
    Returns the snapshot captured at load time.
    """
    store = get_memory_store()
    if not store._loaded:
        store.load_from_disk()
    return store.get_frozen_snapshot()


def get_memory_live_entries() -> List[str]:
    """Get current live memory entries."""
    return get_memory_store().get_live_entries()


def get_memory_usage() -> Dict[str, Any]:
    """Get memory usage stats."""
    return get_memory_store().get_usage()
