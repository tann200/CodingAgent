"""Provider and adapter bootstrap helpers for the orchestrator."""

from __future__ import annotations


from src.core.messaging.event_types import OrchestratorStartup, ProviderUnavailable
import time
from typing import Any

from src.core.inference.llm_manager import (
    _ensure_provider_manager_initialized_sync,
    get_provider_manager,
)
from src.core.logger import logger as guilogger


def _init_providers(orch: Any) -> None:
    """Select and activate the LLM provider adapter.

    On connection failure (e.g. Ollama not running) sets
    ``orch._provider_degraded = True`` and publishes a
    ``provider.unavailable`` event so the TUI can display a banner
    instead of hard-crashing the process.
    """
    pm = None
    try:
        pm = get_provider_manager()
        if pm:
            _wire_provider_manager(orch, pm)
            _select_default_adapter(orch, pm)
            if orch._adapter is not None:
                try:
                    from src.core.orchestration.orchestrator_helpers import (
                        _publish_active_config_impl,
                    )

                    _publish_active_config_impl(orch)
                except Exception:
                    pass
            else:
                orch._provider_degraded = True
                _publish_provider_unavailable(orch, "No adapter selected — no active provider found")
    except Exception as exc:
        orch._provider_degraded = True
        _publish_provider_unavailable(orch, f"Provider init failed: {exc}")

    _publish_startup_events(orch, pm)


def _wire_provider_manager(orch: Any, pm: Any) -> None:
    """Attach the orchestrator event bus to the provider manager and initialize it.

    If the provider manager already has an event bus (e.g. set by the TUI or a test),
    the orchestrator adopts that bus instead of replacing it. This ensures subscriptions
    on the provider-manager bus also receive orchestrator events.
    """
    if getattr(pm, "_event_bus", None) is None:
        pm.set_event_bus(orch.event_bus)
    else:
        orch.event_bus = getattr(pm, "_event_bus")
    _ensure_provider_manager_initialized_sync()


def _select_default_adapter(orch: Any, pm: Any) -> None:
    """Pick the default adapter when the orchestrator has not been given one."""
    if orch._adapter is not None:
        return

    providers = pm.list_providers()
    guilogger.info(f"Orchestrator init: available providers: {providers}")
    if not providers:
        return

    active_name = None
    try:
        active_name = pm.get_active_provider_name()
    except Exception:
        pass

    if active_name and active_name in providers:
        name = active_name
    elif "lm_studio" in providers:
        name = "lm_studio"
    else:
        name = providers[0]

    orch._adapter = pm.get_provider(name)
    guilogger.info(
        f"Orchestrator init: picked adapter: {name}, adapter: {orch._adapter}"
    )


def _publish_startup_events(orch: Any, pm: Any) -> None:
    """Publish orchestrator startup events to the active event buses."""
    try:
        payload = {"time": time.time(), "working_dir": str(orch.working_dir)}
        try:
            guilogger.info("Orchestrator: publishing startup to orch.event_bus")
            orch.event_bus.publish_typed(
                OrchestratorStartup(time=payload["time"], working_dir=payload["working_dir"])
            )
        except Exception:
            pass

        try:
            pm_bus = getattr(pm, "_event_bus", None)
            if pm_bus and pm_bus is not orch.event_bus:
                guilogger.info("Orchestrator: publishing startup to pm_bus")
                pm_bus.publish_typed(
                    OrchestratorStartup(time=payload["time"], working_dir=payload["working_dir"])
                )
        except Exception:
            pass
    except Exception:
        pass


def _publish_provider_unavailable(orch: Any, reason: str) -> None:
    """Publish a provider.unavailable event so the TUI can show a banner."""
    try:
        orch.event_bus.publish_typed(ProviderUnavailable(reason=reason))
        guilogger.warning("Provider unavailable: %s", reason)
    except Exception:
        pass
