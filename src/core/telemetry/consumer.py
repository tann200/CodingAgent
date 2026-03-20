from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_DEFAULT_BACKUP_COUNT = 3


class TelemetryConsumer:
    """Consumes event bus telemetry and writes JSON-lines to a rotating file.

    Usage:
        consumer = TelemetryConsumer(event_bus, path=Path('output/telemetry.jsonl'))

    Args:
        max_bytes:    Rotate when the file exceeds this size (default 5 MB).
        backup_count: Number of backup files to keep (e.g. telemetry.jsonl.1/.2/.3).
    """

    def __init__(
        self,
        event_bus: Any,
        path: Path,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = _DEFAULT_BACKUP_COUNT,
    ):
        self.event_bus = event_bus
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        # Ensure parent exists
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        # Subscribe to known telemetry events
        try:
            self.event_bus.subscribe('message.truncation', self._on_message_truncation)
        except Exception:
            pass
        try:
            self.event_bus.subscribe('model.routing', self._on_model_routing)
        except Exception:
            pass
        # Tool lifecycle telemetry
        try:
            self.event_bus.subscribe('tool.execute.start', self._on_tool_execute_start)
        except Exception:
            pass
        try:
            self.event_bus.subscribe('tool.execute.finish', self._on_tool_execute_finish)
        except Exception:
            pass
        try:
            self.event_bus.subscribe('tool.execute.error', self._on_tool_execute_error)
        except Exception:
            pass
        # Preflight checks
        try:
            self.event_bus.subscribe('tool.preflight', self._on_tool_preflight)
        except Exception:
            pass

    def _rotate(self) -> None:
        """Rotate telemetry file when it exceeds max_bytes."""
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
            # Shift old backups: .3 deleted, .2 → .3, .1 → .2, current → .1
            for i in range(self.backup_count, 0, -1):
                src = Path(f"{self.path}.{i}")
                dst = Path(f"{self.path}.{i + 1}")
                if src.exists():
                    if i == self.backup_count:
                        src.unlink()
                    else:
                        src.rename(dst)
            self.path.rename(Path(f"{self.path}.1"))
        except Exception:
            pass

    def _write_line(self, event_name: str, payload: Any) -> None:
        record = {
            'ts': time.time(),
            'event': event_name,
            'payload': payload,
        }
        try:
            self._rotate()
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
        except Exception:
            # best-effort; don't raise
            pass

    def _on_message_truncation(self, payload: Any) -> None:
        self._write_line('message.truncation', payload)

    def _on_model_routing(self, payload: Any) -> None:
        self._write_line('model.routing', payload)

    def _on_tool_execute_start(self, payload: Any) -> None:
        self._write_line('tool.execute.start', payload)

    def _on_tool_execute_finish(self, payload: Any) -> None:
        self._write_line('tool.execute.finish', payload)

    def _on_tool_execute_error(self, payload: Any) -> None:
        self._write_line('tool.execute.error', payload)

    def _on_tool_preflight(self, payload: Any) -> None:
        self._write_line('tool.preflight', payload)
