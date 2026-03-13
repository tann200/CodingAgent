from __future__ import annotations
from typing import Any, Dict
import threading
import time


class TelemetryMetrics:
    """Simple in-memory metrics collector with Prometheus-style text export.

    Subscribes to EventBus events and maintains counters and simple gauges.
    This is intentionally dependency-free and suitable for tests/CI.
    """

    def __init__(self, event_bus: Any = None):
        self._lock = threading.RLock()
        self.counters: Dict[str, int] = {}
        self.gauges: Dict[str, float] = {}
        self.histograms: Dict[str, list] = {}
        self.event_bus = event_bus
        if event_bus is not None:
            try:
                event_bus.subscribe('message.truncation', self._on_message_truncation)
                event_bus.subscribe('model.routing', self._on_model_routing)
                event_bus.subscribe('tool.execute.start', self._on_tool_execute_start)
                event_bus.subscribe('tool.execute.finish', self._on_tool_execute_finish)
                event_bus.subscribe('tool.execute.error', self._on_tool_execute_error)
                event_bus.subscribe('tool.preflight', self._on_tool_preflight)
            except Exception:
                pass

    def _inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + amount

    def _set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = float(value)

    def _observe(self, name: str, value: float) -> None:
        with self._lock:
            self.histograms.setdefault(name, []).append(float(value))

    # Event handlers
    def _on_message_truncation(self, payload: Any) -> None:
        dropped = int(payload.get('dropped_count', 0)) if isinstance(payload, dict) else 0
        dropped_tokens = int(payload.get('dropped_tokens', 0)) if isinstance(payload, dict) else 0
        tokens_after = float(payload.get('tokens_after', 0)) if isinstance(payload, dict) else 0
        self._inc('message_truncation_total', dropped if dropped > 0 else 1)
        self._observe('message_truncation_dropped_tokens', dropped_tokens)
        self._set_gauge('message_truncation_tokens_after', tokens_after)

    def _on_model_routing(self, payload: Any) -> None:
        # payload expected to contain 'provider' and 'selected'
        self._inc('model_routing_total', 1)
        selected = None
        try:
            if isinstance(payload, dict):
                selected = payload.get('selected')
        except Exception:
            selected = None
        if selected:
            self._inc(f'model_routing_selected_{self._sanitize(selected)}', 1)

    def _on_tool_execute_start(self, payload: Any) -> None:
        self._inc('tool_execute_start_total', 1)

    def _on_tool_execute_finish(self, payload: Any) -> None:
        self._inc('tool_execute_finish_total', 1)

    def _on_tool_execute_error(self, payload: Any) -> None:
        self._inc('tool_execute_error_total', 1)

    def _on_tool_preflight(self, payload: Any) -> None:
        # payload contains preflight details; count failures vs total
        ok = True
        try:
            if isinstance(payload, dict):
                ok = payload.get('details', {}).get('validated_args', True)
        except Exception:
            ok = True
        self._inc('tool_preflight_total', 1)
        if not ok:
            self._inc('tool_preflight_fail_total', 1)

    def _sanitize(self, s: str) -> str:
        return ''.join([c if c.isalnum() or c in ('_', '-') else '_' for c in str(s)])

    def export_text(self) -> str:
        """Export metrics in a simple Prometheus text exposition format."""
        lines = []
        with self._lock:
            # counters
            for k, v in sorted(self.counters.items()):
                lines.append(f"# TYPE {k} counter")
                lines.append(f"{k} {v}")
            # gauges
            for k, v in sorted(self.gauges.items()):
                lines.append(f"# TYPE {k} gauge")
                lines.append(f"{k} {v}")
            # histograms -> output count and sum
            for k, vals in sorted(self.histograms.items()):
                cnt = len(vals)
                s = sum(vals) if vals else 0.0
                lines.append(f"# TYPE {k} histogram")
                lines.append(f"{k}_count {cnt}")
                lines.append(f"{k}_sum {s}")
        lines.append(f"# Generated at {time.time()}")
        return "\n".join(lines)

    # small helper to query individual metric value (useful for tests)
    def get_counter(self, name: str) -> int:
        with self._lock:
            return int(self.counters.get(name, 0))

    def get_gauge(self, name: str) -> float:
        with self._lock:
            return float(self.gauges.get(name, 0.0))

    def get_histogram(self, name: str) -> list:
        with self._lock:
            return list(self.histograms.get(name, []))

