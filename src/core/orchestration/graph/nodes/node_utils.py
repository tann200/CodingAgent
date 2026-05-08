import contextlib
import logging
from typing import Mapping, Any, Optional

logger = logging.getLogger(__name__)

# OTel tracing — shared no-op wrapper so each node file doesn't duplicate this.
try:
    from src.core.telemetry.tracer import span_node as _otel_span_node

    _HAS_TRACER = True
except Exception:
    _otel_span_node = None  # type: ignore[assignment]
    _HAS_TRACER = False


def span_node(name: str, attributes: "dict | None" = None):
    """Thin wrapper: delegates to OTel span_node or returns a no-op context."""
    if _HAS_TRACER and _otel_span_node is not None:
        return _otel_span_node(name, attributes)
    return contextlib.nullcontext()


def _resolve_orchestrator(state: Mapping[str, Any], config: Any) -> Any:
    """Robustly resolve the orchestrator from config or state.
    Accept dict configs, RunnableConfig-like objects, or a direct field in state.
    Also accept a direct Orchestrator-like object passed as `config`.

    Args:
        state: Either a plain dict or AgentState (TypedDict)
        config: Configuration object (can be dict, RunnableConfig, or Orchestrator)

    Returns:
        The orchestrator instance if found, None otherwise
    """
    try:
        # If config already looks like an Orchestrator (has tool_registry/msg_mgr), return it
        try:
            if hasattr(config, "tool_registry") and hasattr(config, "msg_mgr"):
                return config
        except Exception:
            pass

        # If config is a mapping-like object, try to pull configurable.orchestrator
        # Support dicts, pydantic models, RunnableConfig, etc.
        cfg = None
        try:
            if isinstance(config, dict):
                cfg = config.get("configurable") or config
            else:
                # Try attribute access first
                cfg = (
                    getattr(config, "configurable", None)
                    or getattr(config, "config", None)
                    or config
                )
        except Exception:
            cfg = config

        # Now try several ways to extract orchestrator from cfg
        try:
            # If cfg is a dict-like mapping
            if hasattr(cfg, "get"):
                orch = cfg.get("orchestrator")
                if orch:
                    return orch
            # If cfg exposes orchestrator as attribute
            if hasattr(cfg, "orchestrator"):
                orch = getattr(cfg, "orchestrator")
                if orch:
                    return orch
        except Exception:
            pass

        # Fallback: check state for an orchestrator reference
        orch = None
        try:
            # Mapping[str, Any] always has .get()
            orch = state.get("orchestrator") or state.get("_orchestrator")
        except Exception:
            pass
        if orch:
            return orch
    except Exception:
        pass
    return None


def get_current_role(state: Mapping[str, Any], config: Any) -> Optional[str]:
    """
    Get the current role from orchestrator, config, or state.

    Priority:
    1. Orchestrator.current_role
    2. Config.current_role (for SubagentOrchestrator)
    3. State.current_role

    Returns:
        Role string or None if not set
    """
    orch = _resolve_orchestrator(state, config)
    if orch:
        role = getattr(orch, "current_role", None)
        if role:
            return role

    # Check config directly for SubagentOrchestrator
    try:
        if hasattr(config, "current_role"):
            return getattr(config, "current_role")
        cfg = getattr(config, "configurable", None) or config
        if hasattr(cfg, "get"):
            role = cfg.get("current_role")
            if role:
                return role
    except Exception:
        pass

    # Check state
    try:
        return state.get("current_role")  # type: ignore[return-value]
    except Exception:
        pass

    return None


def _notify_provider_limit(error_msg: str) -> None:
    """Send UI notification when provider/context limit is reached."""
    error_lower = error_msg.lower()
    if any(
        x in error_lower
        for x in [
            "disconnected",
            "connection",
            "timeout",
            "memory",
            "slot",
            "batch",
            "kv cache",
            "context",
            "attention",
            "memory slot",
            "ubatch",
            "total tokens",
        ]
    ):
        try:
            from src.core.orchestration.event_bus import get_event_bus

            get_event_bus()
        except Exception as _eb_err:
            logger.debug("node_utils: event_bus publish failed: %s", _eb_err)
