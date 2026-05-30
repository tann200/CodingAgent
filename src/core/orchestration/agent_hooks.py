"""
agent_hooks.py — Agent lifecycle hook system.

Provides a mechanism for registering and invoking hooks at various points
in the agent execution lifecycle, allowing for extensibility without
modifying core code.
"""

import logging
from typing import Dict, List, Callable, Any, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class HookPoint(Enum):
    """Points in the agent lifecycle where hooks can be registered."""

    # Task lifecycle
    TASK_START = "task_start"
    TASK_END = "task_end"
    TASK_ERROR = "task_error"

    # Perception node
    PERCEPTION_START = "perception_start"
    PERCEPTION_END = "perception_end"

    # Planning node
    PLANNING_START = "planning_start"
    PLANNING_END = "planning_end"

    # Execution node
    EXECUTION_START = "execution_start"
    EXECUTION_END = "execution_end"
    EXECUTION_ERROR = "execution_error"

    # Tool usage
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_TOOL_USE_DENIED = "pre_tool_use_denied"

    # Memory operations
    MEMORY_SAVE = "memory_save"
    MEMORY_SEARCH = "memory_search"

    # LLM calls
    PRE_LLM_CALL = "pre_llm_call"
    POST_LLM_CALL = "post_llm_call"

    # System events
    AGENT_START = "agent_start"
    AGENT_STOP = "agent_stop"
    CONFIG_RELOADED = "config_reloaded"


@dataclass
class HookRegistration:
    """Registration for a hook at a specific point."""

    hook_point: HookPoint
    callback: Callable[[Dict[str, Any]], Any]
    priority: int = 0  # Higher priority runs first
    name: Optional[str] = None  # For identification/debugging
    enabled: bool = True

    def __post_init__(self):
        if self.name is None:
            self.name = f"{self.hook_point.value}_{id(self.callback)}"


class AgentHooks:
    """Central agent hook system."""

    def __init__(self):
        self._hooks: Dict[HookPoint, List[HookRegistration]] = {
            point: [] for point in HookPoint
        }
        self._hook_counter = 0

    def register_hook(
        self,
        hook_point: HookPoint,
        callback: Callable[[Dict[str, Any]], Any],
        priority: int = 0,
        name: Optional[str] = None,
        enabled: bool = True,
    ) -> str:
        """
        Register a hook at a specific point.

        Args:
            hook_point: Point in lifecycle to register hook
            callback: Function to call when hook is triggered
            priority: Higher priority hooks run first (default: 0)
            name: Optional name for the hook (auto-generated if not provided)
            enabled: Whether the hook is initially enabled

        Returns:
            Hook ID that can be used to unregister the hook
        """
        hook_id = f"hook_{self._hook_counter}"
        self._hook_counter += 1

        registration = HookRegistration(
            hook_point=hook_point,
            callback=callback,
            priority=priority,
            name=name or hook_id,
            enabled=enabled,
        )

        self._hooks[hook_point].append(registration)
        # Sort by priority (descending) so higher priority runs first
        self._hooks[hook_point].sort(key=lambda r: r.priority, reverse=True)

        logger.debug(
            f"Registered hook '{registration.name}' at {hook_point.value} "
            f"(priority: {priority})"
        )
        return hook_id

    def unregister_hook(self, hook_id: str) -> bool:
        """
        Unregister a hook by ID.

        Args:
            hook_id: ID returned by register_hook

        Returns:
            True if hook was found and removed, False otherwise
        """
        for hook_point, hooks in self._hooks.items():
            for i, hook in enumerate(hooks):
                # Match by exact hook name/ID only — the startswith("hook_")
                # fallback was removed because it deleted the first hook in the
                # iteration instead of the target hook (e.g. unregister("hook_5")
                # would delete hook_0).
                if hook.name == hook_id:
                    del self._hooks[hook_point][i]
                    logger.debug(
                        f"Unregistered hook '{hook.name}' at {hook_point.value}"
                    )
                    return True
        return False

    def enable_hook(self, hook_id: str) -> bool:
        """
        Enable a hook by ID.

        Args:
            hook_id: ID returned by register_hook

        Returns:
            True if hook was found and enabled, False otherwise
        """
        for hooks in self._hooks.values():
            for hook in hooks:
                if hook.name == hook_id:
                    hook.enabled = True
                    logger.debug(f"Enabled hook '{hook.name}'")
                    return True
        return False

    def disable_hook(self, hook_id: str) -> bool:
        """
        Disable a hook by ID.

        Args:
            hook_id: ID returned by register_hook

        Returns:
            True if hook was found and disabled, False otherwise
        """
        for hooks in self._hooks.values():
            for hook in hooks:
                if hook.name == hook_id:
                    hook.enabled = False
                    logger.debug(f"Disabled hook '{hook.name}'")
                    return True
        return False

    def trigger_hooks(
        self, hook_point: HookPoint, context: Dict[str, Any]
    ) -> List[Any]:
        """
        Trigger all hooks at a specific point.

        Args:
            hook_point: Point in lifecycle to trigger hooks for
            context: Context data to pass to hooks

        Returns:
            List of results from all hooks (in priority order)
        """
        results = []
        hooks = self._hooks.get(hook_point, [])

        for hook in hooks:
            if not hook.enabled:
                continue

            try:
                result = hook.callback(context)
                results.append(result)
                logger.debug(
                    f"Hook '{hook.name}' at {hook_point.value} executed successfully"
                )
            except Exception as e:
                logger.error(f"Hook '{hook.name}' at {hook_point.value} failed: {e}")
                # Continue executing other hooks even if one fails

        return results

    def list_hooks(
        self, hook_point: Optional[HookPoint] = None
    ) -> Dict[HookPoint, List[HookRegistration]]:
        """
        List all registered hooks.

        Args:
            hook_point: Optional specific hook point to list hooks for

        Returns:
            Dictionary mapping hook points to their registrations
        """
        if hook_point:
            return {hook_point: self._hooks.get(hook_point, [])}
        return self._hooks.copy()

    def clear_hooks(self, hook_point: Optional[HookPoint] = None) -> None:
        """
        Clear all hooks, or hooks at a specific point.

        Args:
            hook_point: Optional specific hook point to clear (None for all)
        """
        if hook_point:
            self._hooks[hook_point].clear()
            logger.debug(f"Cleared all hooks at {hook_point.value}")
        else:
            for point in self._hooks:
                self._hooks[point].clear()
            logger.debug("Cleared all hooks")


# Global agent hooks instance
_agent_hooks: Optional[AgentHooks] = None


def get_agent_hooks() -> AgentHooks:
    """Get the global agent hooks instance."""
    global _agent_hooks
    if _agent_hooks is None:
        _agent_hooks = AgentHooks()
    return _agent_hooks


def register_agent_hook(
    hook_point: HookPoint,
    callback: Callable[[Dict[str, Any]], Any],
    priority: int = 0,
    name: Optional[str] = None,
    enabled: bool = True,
) -> str:
    """
    Register a hook with the global agent hooks system.

    Convenience function that delegates to get_agent_hooks().register_hook().
    """
    return get_agent_hooks().register_hook(
        hook_point=hook_point,
        callback=callback,
        priority=priority,
        name=name,
        enabled=enabled,
    )


def trigger_agent_hooks(hook_point: HookPoint, context: Dict[str, Any]) -> List[Any]:
    """
    Trigger hooks with the global agent hooks system.

    Convenience function that delegates to get_agent_hooks().trigger_hooks().
    """
    return get_agent_hooks().trigger_hooks(hook_point=hook_point, context=context)


if __name__ == "__main__":
    # Simple test
    hooks = AgentHooks()

    def test_callback(context):
        print(f"Hook executed with context: {context}")
        return "hook_result"

    # Register a hook
    hook_id = hooks.register_hook(
        HookPoint.TASK_START, test_callback, priority=10, name="test_hook"
    )

    # Trigger the hook
    results = hooks.trigger_hooks(HookPoint.TASK_START, {"test": "data"})
    print(f"Hook results: {results}")

    # Unregister the hook
    hooks.unregister_hook(hook_id)

    # Trigger again (should be empty)
    results = hooks.trigger_hooks(HookPoint.TASK_START, {"test": "data"})
    print(f"Hook results after unregister: {results}")
