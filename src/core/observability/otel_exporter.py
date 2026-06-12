"""OpenTelemetry export for CodingAgent EventBus events.

Usage:
    from src.core.observability.otel_exporter import OtelExporter
    exporter = OtelExporter()
    exporter.subscribe(event_bus)

Activation:
    Set ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or ``OTEL_SERVICE_NAME``) env var to
    enable.  Without it the exporter is a silent no-op.

Events mapped:
    - ``tool.call.start`` / ``tool.call.finish`` / ``tool.call.error`` → spans
    - ``agent.message`` → span
    - ``response.stream_chunk`` → span events (aggregated per-turn)
    - ``provider.status.changed`` → span attributes
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any


class OtelExporter:
    """Maps CodingAgent EventBus events to OpenTelemetry spans.

    All imports from ``opentelemetry`` are deferred so the module is safe to
    import even when the optional dependencies are not installed.  The
    ``subscribe()`` method is a no-op unless ``OTEL_EXPORTER_OTLP_ENDPOINT``
    is set.
    """

    def __init__(self, service_name: str | None = None) -> None:
        self._endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        self._service_name = service_name or os.environ.get(
            "OTEL_SERVICE_NAME", "codingagent"
        )
        self._enabled = bool(self._endpoint)
        self._tracer: Any = None
        self._active_spans: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._turn_span: Any = None
        self._turn_id: str | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def subscribe(self, event_bus: Any) -> None:
        """Subscribe to EventBus events.

        Safe to call even when OTel is disabled — this is a no-op when
        ``self._enabled`` is ``False``.
        """
        if not self._enabled:
            return
        self._init_opentelemetry()
        if not self._tracer:
            return
        event_bus.subscribe("tool.call.start", self._on_tool_start)
        event_bus.subscribe("tool.call.finish", self._on_tool_finish)
        event_bus.subscribe("tool.call.error", self._on_tool_error)
        event_bus.subscribe("agent.message", self._on_agent_message)
        event_bus.subscribe("response.stream_chunk", self._on_stream_chunk)
        event_bus.subscribe(
            "provider.status.changed", self._on_provider_status
        )

    # ── Internal: OTel initialisation ─────────────────────────────────────

    def _init_opentelemetry(self) -> None:
        try:
            from opentelemetry import trace  # type: ignore[import-not-found]
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]

            resource = Resource.create(
                {"service.name": self._service_name}
            )
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=self._endpoint)
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer(__name__)
        except Exception:
            pass

    # ── Event handlers ─────────────────────────────────────────────────────

    def _on_tool_start(self, payload: Any) -> None:
        if not self._enabled or self._tracer is None:
            return
        try:
            tool_name = (
                payload.get("tool_name")
                if isinstance(payload, dict)
                else str(payload)
            )
            span = self._tracer.start_span(f"tool.{tool_name}")
            span.set_attribute("tool.name", tool_name)
            corr_id = (
                payload.get("correlation_id")
                if isinstance(payload, dict)
                else None
            )
            if corr_id:
                span.set_attribute("correlation_id", corr_id)
            key = f"tool:{tool_name}:{time.monotonic_ns()}"
            with self._lock:
                self._active_spans[key] = span
        except Exception:
            pass

    def _on_tool_finish(self, payload: Any) -> None:
        if not self._enabled or self._tracer is None:
            return
        try:
            tool_name = (
                payload.get("tool_name")
                if isinstance(payload, dict)
                else ""
            ) or ""
            # Match the most recent active span for this tool
            key = self._find_tool_span_key(tool_name)
            if key is None:
                return
            with self._lock:
                span = self._active_spans.pop(key, None)
            if span is not None:
                if isinstance(payload, dict):
                    duration = payload.get("duration_ms")
                    if duration is not None:
                        span.set_attribute("tool.duration_ms", duration)
                    result = payload.get("result", "")
                    if result:
                        span.set_attribute("tool.result", str(result)[:200])
                span.end()
        except Exception:
            pass

    def _on_tool_error(self, payload: Any) -> None:
        if not self._enabled or self._tracer is None:
            return
        try:
            tool_name = (
                payload.get("tool_name")
                if isinstance(payload, dict)
                else ""
            ) or ""
            key = self._find_tool_span_key(tool_name)
            if key is None:
                return
            with self._lock:
                span = self._active_spans.pop(key, None)
            if span is not None:
                error = (
                    payload.get("error") if isinstance(payload, dict) else str(payload)
                )
                span.set_attribute("error", str(error)[:500])
                span.set_attribute("tool.success", False)
                span.end()
        except Exception:
            pass

    def _on_agent_message(self, payload: Any) -> None:
        if not self._enabled or self._tracer is None:
            return
        try:
            role = (
                payload.get("role") if isinstance(payload, dict) else "unknown"
            )
            content_len = (
                len(payload.get("content", ""))
                if isinstance(payload, dict)
                else 0
            )
            span = self._tracer.start_span(f"agent.{role}")
            span.set_attribute("agent.role", role)
            span.set_attribute("agent.content_length", content_len)
            span.end()
        except Exception:
            pass

    def _on_stream_chunk(self, payload: Any) -> None:
        if not self._enabled or self._tracer is None:
            return
        # We do not create spans per chunk — too noisy. Instead we add a
        # single event on the turn span when present.
        if self._turn_span is not None:
            try:
                chunk_len = len(payload) if isinstance(payload, str) else 0
                self._turn_span.add_event(
                    "stream_chunk", {"length": chunk_len}
                )
            except Exception:
                pass

    def _on_provider_status(self, payload: Any) -> None:
        if not self._enabled or self._tracer is None:
            return
        try:
            if isinstance(payload, dict):
                span = self._tracer.start_span("provider.status_changed")
                for key in ("provider", "provider_family", "status", "model"):
                    val = payload.get(key)
                    if val is not None:
                        span.set_attribute(f"provider.{key}", str(val))
                span.end()
        except Exception:
            pass

    # ── Helpers ────────────────────────────────────────────────────────────

    def _find_tool_span_key(self, tool_name: str) -> str | None:
        """Return the span key matching *tool_name* (LIFO order)."""
        with self._lock:
            candidates = [
                k for k in self._active_spans if k.startswith(f"tool:{tool_name}:")
            ]
            if not candidates:
                return None
            candidates.sort(reverse=True)
            return candidates[0]

    def set_turn_context(self, turn_id: str) -> None:
        """Optionally create a top-level turn span."""
        if not self._enabled or self._tracer is None:
            return
        self._turn_id = turn_id
        self._turn_span = self._tracer.start_span(f"turn.{turn_id}")
        self._turn_span.set_attribute("turn_id", turn_id)

    def end_turn(self) -> None:
        if self._turn_span is not None:
            self._turn_span.end()
            self._turn_span = None
            self._turn_id = None

    def flush(self) -> None:
        """Force all buffered spans to be exported."""
        if not self._enabled:
            return
        try:
            from opentelemetry import trace  # type: ignore[import-not-found]
            provider = trace.get_tracer_provider()
            if hasattr(provider, "force_flush"):
                provider.force_flush()
        except Exception:
            pass
