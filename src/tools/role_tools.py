import threading
from typing import Dict, Any, Optional

# Simple in-memory role holder for the orchestrator; in real usage this could be part of Orchestrator state
_current_role: Optional[str] = None
_role_lock = threading.Lock()


def set_role(role: str, orchestrator: Optional[Any] = None) -> Dict[str, Any]:
    """Set the global in-memory role and optionally apply it to a provided orchestrator instance.

    Args:
        role: role name to set
        orchestrator: optional Orchestrator instance; if provided and it has attribute current_role, set it.
    """
    global _current_role
    with _role_lock:
        _current_role = role
    try:
        if orchestrator is not None:
            # apply to orchestrator if it supports current_role
            try:
                setattr(orchestrator, "current_role", role)
            except Exception:
                pass
    except Exception:
        pass

    # Publish role change event (best-effort)
    try:
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()
        try:
            # CONC-2: Emit once via publish_with_identity only, which internally
            # calls publish — emitting both caused double-firing on all subscribers.
            bus.publish_with_identity(
                "role.changed", {"role": role}, sender_id="role_tools"
            )
        except Exception:
            # fallback to plain publish if publish_with_identity is unavailable
            try:
                bus.publish("role.changed", {"role": role})
            except Exception:
                pass
    except Exception:
        pass

    return {"status": "ok", "role": role}


def get_role() -> Dict[str, Any]:
    with _role_lock:
        role = _current_role
    return {"status": "ok", "role": role}
