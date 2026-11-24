"""registry_builder.py — Default tool registry factory.

TASK-22: ``example_registry()`` delegates directly to the sole authoritative
``src.tools._registry.build_registry(include_echo=True)`` without copying
entries into a second registry class or maintaining a duplicate manual fallback.
"""

from __future__ import annotations

from src.tools._registry import ToolRegistry, build_registry


def example_registry() -> ToolRegistry:
    """Build and return the default ``ToolRegistry``."""
    return build_registry(include_echo=True)


__all__ = ["example_registry", "ToolRegistry"]
