"""Plugin hook system for CodingAgent (Gap 3).

Import the global registry and hook name constants from this package::

    from src.core.plugin import registry, HOOK_CONTEXT_BUILT, HOOK_TOOL_RESULT
    registry.register(HOOK_CONTEXT_BUILT, my_fn)
"""

from src.core.plugin.hook_registry import (
    HookRegistry,
    HOOK_CONTEXT_BUILT,
    HOOK_TOOL_RESULT,
    HOOK_LLM_RESPONSE,
    HOOK_ROUND_END,
    HOOK_SESSION_START,
    registry,
)

__all__ = [
    "HookRegistry",
    "HOOK_CONTEXT_BUILT",
    "HOOK_TOOL_RESULT",
    "HOOK_LLM_RESPONSE",
    "HOOK_ROUND_END",
    "HOOK_SESSION_START",
    "registry",
]
