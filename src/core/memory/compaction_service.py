"""CompactionService — compatibility facade over ``CompactionEngine`` (TASK-24)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.memory.compaction_engine import CompactionEngine, CompactionResult


class CompactionService(CompactionEngine):
    """Backward-compatible alias for :class:`CompactionEngine`."""

    def __init__(
        self,
        history: List[Dict[str, Any]],
        working_dir: Optional[Path] = None,
        event_bus: Optional[Any] = None,
        prefer_deterministic: bool = False,
        compact_threshold: float = 0.85,
    ) -> None:
        super().__init__(
            history=history,
            working_dir=working_dir,
            event_bus=event_bus,
            prefer_deterministic=prefer_deterministic,
            compact_threshold=compact_threshold,
        )


__all__ = ["CompactionEngine", "CompactionResult", "CompactionService"]
