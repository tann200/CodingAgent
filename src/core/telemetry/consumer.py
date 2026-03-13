from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any


class TelemetryConsumer:
    """Consumes event bus telemetry and writes JSON-lines to a file.

    Usage:
        consumer = TelemetryConsumer(event_bus, path=Path('output/telemetry.jsonl'))
    """

    def __init__(self, event_bus: Any, path: Path):
        self.event_bus = event_bus
        self.path = Path(path)
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

    def _write_line(self, event_name: str, payload: Any) -> None:
        record = {
            'ts': time.time(),
            'event': event_name,
            'payload': payload,
        }
        try:
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
