import logging
from typing import Any, Dict, Union

from src.core.orchestration.graph.state import AgentState

logger = logging.getLogger(__name__)


def _resolve_orchestrator(state: Union[Dict[str, Any], AgentState], config: Any) -> Any:
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
            # Handle both dict and AgentState (TypedDict)
            if isinstance(state, dict):
                orch = state.get("orchestrator") or state.get("_orchestrator")
            elif hasattr(state, "get"):
                # AgentState is a TypedDict which supports .get()
                orch = state.get("orchestrator") or state.get("_orchestrator")
            elif hasattr(state, "orchestrator"):
                orch = state.orchestrator
        except Exception:
            pass
        if orch:
            return orch
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

            bus = get_event_bus()
            bus.publish(
                "ui.notification",
                {
                    "level": "warning",
                    "message": error_msg,
                    "source": "provider",
                },
            )
        except Exception:
            pass
