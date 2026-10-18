"""Config reload registration for the orchestrator bootstrap."""

from __future__ import annotations

from typing import Any, Optional

import weakref

from src.core.logger import logger as guilogger
from src.core.messaging.event_types import ConfigReloaded
from src.core.orchestration.orchestrator_scheduler import _init_scheduler


def register_config_reload_handlers(orch: Any) -> None:
    """Register a callback with the ConfigReloader to refresh runtime state.

    This function is intentionally safe to call in tests and avoids heavy
    imports until the callback actually runs.
    """
    # Avoid registering the same callback multiple times for the same
    # orchestrator instance. Use a weak set to avoid leaking memory when
    # orchestrator instances are garbage collected.
    try:
        if not hasattr(register_config_reload_handlers, "_registered"):
            register_config_reload_handlers._registered = weakref.WeakSet()  # type: ignore[attr-defined]
        if orch in register_config_reload_handlers._registered:  # type: ignore[attr-defined]
            return
        register_config_reload_handlers._registered.add(orch)  # type: ignore[attr-defined]
    except Exception:
        # If weakref operations fail for any reason, proceed (best-effort).
        pass

    try:
        from src.core.config_hot_reload import get_config_reloader

        def _callback(changed_paths: Optional[set]) -> None:
            _on_config_reloaded(orch, changed_paths)

        try:
            get_config_reloader(initial_load=False).add_callback(_callback)
        except Exception:
            guilogger.debug("Failed to register config reloader callback")
    except Exception:
        # No config reloader available; skip registration silently
        pass


def _on_config_reloaded(orch: Any, changed_paths: Optional[set]) -> None:
    guilogger.info("Orchestrator: config reloader callback invoked")
    # 1) Reload AgentBrainManager caches
    try:
        from src.core.orchestration.agent_brain import get_agent_brain_manager

        try:
            get_agent_brain_manager().reload()
        except Exception:
            guilogger.warning(
                "AgentBrainManager.reload failed during config reload"
            )
    except Exception:
        guilogger.debug("AgentBrainManager not available to reload")

    # 2) Clear toolset loader cache so updated YAMLs are re-read
    try:
        from src.config.toolsets import loader as _ts_loader

        try:
            _ts_loader.clear_cache()
        except Exception:
            guilogger.warning(
                "toolsets.loader.clear_cache failed during config reload"
            )
    except Exception:
        guilogger.debug("toolsets.loader not available to clear cache")

    # 3) Rebuild the tool registry and replace orch.tool_registry
    try:
        from src.core.orchestration.registry_builder import example_registry

        try:
            new_reg = example_registry()
            if new_reg:
                orch.tool_registry = new_reg
                guilogger.info(
                    "Orchestrator: tool registry rebuilt from config reload"
                )
                # Re-apply project tool overrides
                _wd_str = str(getattr(orch, "working_dir", ""))
                if _wd_str:
                    try:
                        from src.core.config_loader import get_project_tool_overrides
                        _tool_overrides = get_project_tool_overrides(_wd_str)
                        for _tool_name, _enabled in _tool_overrides.items():
                            if not _enabled:
                                try:
                                    orch.tool_registry.deregister(_tool_name)
                                except Exception:
                                    pass
                    except Exception:
                        pass

                # Re-register MCP tools into new registry
                mcp_mgr = getattr(orch, "mcp_manager", None) or getattr(orch, "_mcp_manager", None)
                if mcp_mgr and hasattr(mcp_mgr, "re_register_tools"):
                    try:
                        import asyncio
                        res = mcp_mgr.re_register_tools(orch.tool_registry)
                        if asyncio.iscoroutine(res):
                            try:
                                loop = asyncio.get_running_loop()
                                loop.create_task(res)
                            except RuntimeError:
                                asyncio.run(res)
                    except Exception as mcp_exc:
                        guilogger.debug("MCP tools re-registration failed: %s", mcp_exc)
        except Exception:
            guilogger.warning(
                "Failed to rebuild tool registry during config reload"
            )
    except Exception:
        guilogger.debug("registry_builder.example_registry not available")

    # 4) Reinitialize providers so provider changes take effect
    try:
        from src.core.inference.llm_manager import (
            reload_provider_manager_sync,
            get_provider_manager,
            _ensure_provider_manager_initialized_sync,
        )

        try:
            pm = get_provider_manager()
            if pm:
                if getattr(pm, "_event_bus", None) is None:
                    pm.set_event_bus(orch.event_bus)
                if hasattr(pm, "reload"):
                    reload_provider_manager_sync()
                else:
                    _ensure_provider_manager_initialized_sync()
        except Exception:
            _ensure_provider_manager_initialized_sync()
            guilogger.warning(
                "ProviderManager reload failed during config reload"
            )
    except Exception:
        guilogger.debug("llm_manager ProviderManager not available for reload")

    # 5) Publish a top-level event so UIs or other subsystems can react.
    try:
        orch.event_bus.publish_typed(ConfigReloaded(changed_paths=list(changed_paths or [])))
    except Exception:
        guilogger.debug(
            "Failed to publish orchestrator-level config.reloaded event"
        )

    # 6) Restart scheduler to pick up config changes safely
    try:
        # Stop existing scheduler (best-effort) then re-initialize.
        try:
            _sched = getattr(orch, "_scheduler", None)
            if _sched is not None:
                try:
                    _sched.stop_scheduler()
                except Exception:
                    pass
                try:
                    if hasattr(_sched, "clear_jobs"):
                        _sched.clear_jobs()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            try:
                _init_scheduler(orch)
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        pass
