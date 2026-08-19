"""Service-layer bootstrap helpers for the orchestrator."""

from __future__ import annotations

import threading as _threading
from typing import Any

from src.core.logger import logger as guilogger


def _init_services(orch: Any) -> None:
    """Initialise token monitor, preview service, plan mode, cost tracker, and tool service."""
    try:
        _threading.Thread(target=orch._background_model_check, daemon=True).start()
    except Exception:
        try:
            orch._background_model_check()
        except Exception:
            pass

    orch._publish_active_config()

    from src.core.orchestration.token_budget import get_token_budget_monitor

    orch.token_monitor = get_token_budget_monitor()

    from src.core.context.context_controller import get_context_controller

    orch.context_controller = get_context_controller(
        max_tokens=getattr(orch, "_message_max_tokens", None) or 6000
    )

    from src.core.orchestration.preview_service import get_preview_service

    orch.preview_service = get_preview_service(str(orch.working_dir))
    orch._pending_preview_id = None  # type: ignore[attr-defined]

    from src.core.orchestration.plan_mode import PlanMode

    orch.plan_mode = PlanMode(orchestrator=orch)
    orch._plan_approval_event = None  # type: ignore[attr-defined]
    orch._plan_approved = False  # type: ignore[attr-defined]

    orch.explore_mode = False  # type: ignore[attr-defined]
    orch.current_role = None  # type: ignore[attr-defined]
    orch.role_manager = None  # type: ignore[attr-defined]
    orch._permission_gate = None  # type: ignore[attr-defined]
    orch._permission_granted = False  # type: ignore[attr-defined]

    from src.core.orchestration.preview_coordinator import PreviewCoordinator

    orch.preview_coordinator = PreviewCoordinator()
    try:
        orch.preview_coordinator.attach(orch.event_bus)
    except Exception:
        pass

    from src.core.orchestration.project_settings import get_active_settings as _get_ps
    from src.core.orchestration.session_cost_tracker import SessionCostTracker

    _active_ps = _get_ps()
    _budget_ceiling = _active_ps.budget_ceiling_usd if _active_ps is not None else None
    orch.cost_tracker = SessionCostTracker(
        working_dir=orch.working_dir,
        event_bus=orch.event_bus,
        budget_ceiling_usd=_budget_ceiling,
    )

    from src.core.orchestration.tool_execution_service import ToolExecutionService

    _hook_runner = None
    try:
        from src.core.orchestration.tool_hooks import ToolHookRunner

        _hook_runner = ToolHookRunner()
    except Exception:
        pass
    orch.tool_execution_service = ToolExecutionService(
        registry=orch.tool_registry,
        event_bus=orch.event_bus,
        hook_runner=_hook_runner,
    )

    orch._mcp_server = None

    try:
        from src.core.mcp.manager import McpServerManager

        orch._mcp_manager = McpServerManager(
            registry=orch.tool_registry,
            event_bus=orch.event_bus,
            working_dir=orch.working_dir,
        )
        orch.mcp_manager = orch._mcp_manager
    except Exception:
        orch._mcp_manager = None
        orch.mcp_manager = None

    try:
        from src.server.app import ServerEventBusAdapter, run_server
        import os
        import threading

        if os.getenv("CODINGAGENT_HTTP_SERVER", os.getenv("CODING_AGENT_HTTP_SERVER", "false")).lower() == "true":
            import src.server.app as server_app

            try:
                server_app.register_event_bus(orch.event_bus)
            except Exception:
                server_app.event_bus = orch.event_bus
                server_app.sse_adapter = ServerEventBusAdapter(orch.event_bus)

            server_thread = threading.Thread(
                target=lambda: run_server(host="127.0.0.1", port=8000), daemon=True
            )
            server_thread.start()
            orch._http_server_thread = server_thread
            guilogger.info("HTTP/SSE server started on http://127.0.0.1:8000")
        else:
            orch._http_server_thread = None
    except Exception as e:
        guilogger.warning(f"Failed to start HTTP/SSE server: {e}")
        orch._http_server_thread = None

    # P3-5: Conditionally init OpenTelemetry exporter
    try:
        for _var in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SERVICE_NAME"):
            import os
            if os.environ.get(_var):
                from src.core.observability.otel_exporter import OtelExporter

                orch._otel_exporter = OtelExporter()
                orch._otel_exporter.subscribe(orch.event_bus)
                guilogger.info(
                    "OpenTelemetry exporter enabled (endpoint=%s)",
                    orch._otel_exporter._endpoint or "env-default",
                )
                break
    except Exception:
        orch._otel_exporter = None
