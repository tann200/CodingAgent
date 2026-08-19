"""Gap 8: OTel (OpenTelemetry) tracer for CodingAgent.

Design goals
------------
- **Zero-dependency default**: when ``opentelemetry-api`` / ``opentelemetry-sdk``
  are not installed the module provides no-op stubs so every import site works
  unchanged in production and in tests.
- **Opt-in activation**: set ``OTEL_EXPORTER_OTLP_ENDPOINT`` (e.g.
  ``http://localhost:4317``) to enable OTLP export.  Without that env var the
  tracer is a no-op and incurs no overhead.
- **EventBus bridge**: when OTel is available, key CodingAgent EventBus topics
  (``tool.execute.start``, ``tool.execute.finish``, ``tool.execute.error``,
  ``perception.round.start``) are emitted as OTel span events so external
  dashboards (Grafana Tempo, Jaeger, Honeycomb) can correlate them with spans.

Usage
-----
From any graph node or service::

    from src.core.telemetry.tracer import span_node

    async def perception_node(state, config):
        with span_node("perception", {"round": state.get("rounds", 0)}):
            ...  # node body

``span_node`` returns a context manager.  When OTel is disabled it's a no-op.

Installation (optional)
-----------------------
    pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
    export OTEL_SERVICE_NAME=codingagent          # optional, defaults below

Supported exporters (auto-selected from endpoint scheme):
- gRPC OTLP   (``http://host:4317``) — requires ``opentelemetry-exporter-otlp-proto-grpc``
- HTTP OTLP   (``http://host:4318/v1/traces``) — requires ``opentelemetry-exporter-otlp-proto-http``
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OTel availability probe
# ---------------------------------------------------------------------------
_OTEL_AVAILABLE = False
_tracer = None

try:
    from opentelemetry import trace as _ot_trace  # type: ignore[import]
    from opentelemetry.sdk.trace import TracerProvider as _TracerProvider  # type: ignore[import]
    from opentelemetry.sdk.trace.export import BatchSpanProcessor as _BatchSpanProcessor  # type: ignore[import]
    from opentelemetry.sdk.resources import (  # type: ignore[import]
        Resource as _Resource,
        SERVICE_NAME as _SERVICE_NAME,
    )

    _OTEL_AVAILABLE = True
except ImportError:
    _ot_trace = None  # type: ignore[assignment]
    _TracerProvider = None  # type: ignore[assignment]
    _BatchSpanProcessor = None  # type: ignore[assignment]
    _Resource = None  # type: ignore[assignment]
    _SERVICE_NAME = "service.name"

_SERVICE = os.environ.get("OTEL_SERVICE_NAME", "codingagent")


def _build_tracer():
    """Initialise a real OTel tracer when OTel is available and an endpoint is set.

    Returns a ``opentelemetry.trace.Tracer`` or ``None``.
    Only called once (lazily) on the first ``span_node`` call.
    """
    if not _OTEL_AVAILABLE:
        return None

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        # No endpoint → no-op; emit a single debug log so users know why.
        _logger.debug(
            "tracer: OTEL_EXPORTER_OTLP_ENDPOINT not set — OTel tracing disabled"
        )
        return None

    try:
        resource = _Resource.create({_SERVICE_NAME: _SERVICE})  # type: ignore[union-attr]
        provider = _TracerProvider(resource=resource)  # type: ignore[misc]

        # Select exporter based on endpoint.  gRPC endpoints typically use
        # port 4317; HTTP OTLP endpoints use port 4318 or end in /v1/traces.
        exporter = None
        if (
            "4318" in endpoint
            or "/v1/traces" in endpoint
            or endpoint.startswith("https")
        ):
            # HTTP OTLP exporter
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import]
                    OTLPSpanExporter as _HttpExporter,
                )

                exporter = _HttpExporter(endpoint=endpoint)
                _logger.info("tracer: using OTLP/HTTP exporter → %s", endpoint)
            except ImportError:
                _logger.warning(
                    "tracer: opentelemetry-exporter-otlp-proto-http not installed; "
                    "falling back to gRPC"
                )
        if exporter is None:
            # Default: gRPC OTLP exporter
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import]
                    OTLPSpanExporter as _GrpcExporter,
                )

                exporter = _GrpcExporter(endpoint=endpoint, insecure=True)
                _logger.info("tracer: using OTLP/gRPC exporter → %s", endpoint)
            except ImportError:
                _logger.warning(
                    "tracer: no OTLP exporter installed.  Install "
                    "opentelemetry-exporter-otlp-proto-grpc or "
                    "opentelemetry-exporter-otlp-proto-http."
                )
                return None

        provider.add_span_processor(_BatchSpanProcessor(exporter))  # type: ignore[misc]
        _ot_trace.set_tracer_provider(provider)  # type: ignore[union-attr]
        return _ot_trace.get_tracer(_SERVICE)  # type: ignore[union-attr]

    except Exception as exc:
        _logger.warning("tracer: failed to initialise OTel tracer: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_initialised = False


def get_tracer():
    """Return the singleton OTel tracer, initialising it on first call.

    Returns ``None`` when OTel is disabled or the SDK is not installed.
    """
    global _tracer, _initialised
    if not _initialised:
        _initialised = True
        _tracer = _build_tracer()
    return _tracer


@contextmanager
def span_node(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
) -> Generator[Any, None, None]:
    """Context manager that wraps a graph node body in an OTel span.

    Usage::

        with span_node("perception", {"round": 3, "model": "gemma-4-e4b-it"}):
            ...

    When OTel is disabled this is a pure no-op context manager — zero overhead.

    Args:
        name:       Span name (e.g. ``"perception"`` or ``"execution"``).
        attributes: Optional dict of span attributes (strings, ints, floats,
                    bools).  Non-primitive values are coerced to str.
    """
    tracer = get_tracer()
    if tracer is None:
        # No-op path: just yield without creating any span object
        yield None
        return

    # Sanitise attributes — OTel only accepts primitive types.
    safe_attrs: Dict[str, Any] = {}
    if attributes:
        for k, v in attributes.items():
            if isinstance(v, (str, int, float, bool)):
                safe_attrs[k] = v
            elif v is None:
                pass
            else:
                safe_attrs[k] = str(v)

    with tracer.start_as_current_span(
        f"codingagent.{name}", attributes=safe_attrs
    ) as span:
        yield span


def record_event(
    span: Any,
    event_name: str,
    attributes: Optional[Dict[str, Any]] = None,
) -> None:
    """Add an event to *span* (no-op when span is None / OTel disabled).

    Useful for recording discrete events within a span (e.g. a tool call
    within a perception span) without creating a child span.

    Args:
        span:        The span object returned by ``span_node``.
        event_name:  Short name for the event (e.g. ``"tool.call"``).
        attributes:  Optional dict of event attributes.
    """
    if span is None:
        return
    try:
        span.add_event(event_name, attributes=attributes or {})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# EventBus bridge — wire EventBus topics to OTel span events
# ---------------------------------------------------------------------------


def wire_event_bus(event_bus: Any) -> None:
    """Subscribe to key EventBus topics and forward them as OTel span events.

    Call this once after the EventBus is initialised (e.g. from
    ``Orchestrator.__init__``).  Safe to call when OTel is disabled — the
    handler is a no-op when no tracer is active.

    Topics bridged:
        tool.execute.start   → span event ``codingagent.tool.start``
        tool.execute.finish  → span event ``codingagent.tool.finish``
        tool.execute.error   → span event ``codingagent.tool.error``
        perception.complete  → span event ``codingagent.perception.complete``
    """

    def _on_tool_start(payload: dict) -> None:
        _bridge_event("codingagent.tool.start", payload, ["tool_name", "session_id"])

    def _on_tool_finish(payload: dict) -> None:
        _bridge_event(
            "codingagent.tool.finish", payload, ["tool_name", "session_id", "ok"]
        )

    def _on_tool_error(payload: dict) -> None:
        _bridge_event(
            "codingagent.tool.error", payload, ["tool_name", "session_id", "error"]
        )

    def _on_perception(payload: dict) -> None:
        _bridge_event("codingagent.perception.complete", payload, ["round", "model"])

    try:
        event_bus.subscribe("tool.execute.start", _on_tool_start)
        event_bus.subscribe("tool.execute.finish", _on_tool_finish)
        event_bus.subscribe("tool.execute.error", _on_tool_error)
        event_bus.subscribe("perception.complete", _on_perception)
    except Exception as exc:
        _logger.debug("tracer.wire_event_bus: subscribe failed: %s", exc)


def _bridge_event(event_name: str, payload: dict, keys: list) -> None:
    """Extract *keys* from *payload* and add an event to the current span."""
    tracer = get_tracer()
    if tracer is None:
        return
    try:
        current_span = _ot_trace.get_current_span()  # type: ignore[union-attr]
        if current_span is None:
            return
        attrs = {k: str(payload.get(k, "")) for k in keys if k in payload}
        current_span.add_event(event_name, attributes=attrs)
    except Exception:
        pass
