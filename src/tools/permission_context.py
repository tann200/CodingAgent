"""
permission_context.py — TASK-09: Tool permission context for allow/deny filtering.

Direct port of claw-code-main/src/permissions.py::ToolPermissionContext.
Supports ``--allowed-tools``, ``--deny-tool``, and ``--deny-prefix`` CLI flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class ToolPermissionContext:
    """Immutable permission context that controls which tools the agent may use.

    Attributes
    ----------
    allow_names:
        If non-empty, only tools whose names are in this set are permitted.
        An empty set means "allow all" (no allowlist restriction).
    deny_names:
        Tools whose names are in this set are always blocked, even if also
        listed in ``allow_names``.
    deny_prefixes:
        Tools whose name starts with any of these (case-insensitive) prefixes
        are blocked.
    """

    allow_names: frozenset[str] = field(default_factory=frozenset)
    deny_names: frozenset[str] = field(default_factory=frozenset)
    deny_prefixes: tuple[str, ...] = ()

    @classmethod
    def from_iterables(
        cls,
        allow_names: Optional[Iterable[str]] = None,
        deny_names: Optional[Iterable[str]] = None,
        deny_prefixes: Optional[Iterable[str]] = None,
    ) -> "ToolPermissionContext":
        """Construct from plain iterables (CLI args lists)."""
        return cls(
            allow_names=frozenset(n.lower() for n in (allow_names or [])),
            deny_names=frozenset(n.lower() for n in (deny_names or [])),
            deny_prefixes=tuple(p.lower() for p in (deny_prefixes or [])),
        )

    def blocks(self, tool_name: str) -> bool:
        """Return True if *tool_name* is denied by this context."""
        lowered = tool_name.lower()
        if lowered in self.deny_names:
            return True
        if any(lowered.startswith(prefix) for prefix in self.deny_prefixes):
            return True
        # allowlist: if non-empty, block anything not in it
        if self.allow_names and lowered not in self.allow_names:
            return True
        return False

    def is_empty(self) -> bool:
        """Return True when this context imposes no restrictions."""
        return not self.allow_names and not self.deny_names and not self.deny_prefixes

    def filter_registry(self, registry: object) -> object:
        """Return a copy of *registry* with blocked tools removed.

        Works with ``src.tools._registry.ToolRegistry`` and any object that
        exposes a ``filter_by_names(names)`` method + a ``list()`` method.
        Falls back gracefully on unexpected types.
        """
        if self.is_empty():
            return registry
        try:
            all_names: list[str] = registry.list()  # type: ignore[union-attr]
            allowed = [n for n in all_names if not self.blocks(n)]
            return registry.filter_by_names(allowed)  # type: ignore[union-attr]
        except Exception:
            return registry


# Module-level active permission context.  Set by ``src/main.py`` before
# launching the orchestrator so that ``Orchestrator.__init__`` can filter
# the tool registry without needing extra constructor params.
_ACTIVE_CONTEXT: Optional[ToolPermissionContext] = None


def get_permission_context() -> Optional[ToolPermissionContext]:
    """Return the currently active ToolPermissionContext, if any.

    This simple accessor is provided so callers (and tests) can query the
    active permission context without importing internal module variables.
    """
    return _ACTIVE_CONTEXT


def set_permission_context(ctx: Optional[ToolPermissionContext]) -> None:
    """Set the module-level active permission context.

    Exposed mainly for tests and bootstrap code to configure the active
    permission filtering used by PermissionGateway.
    """
    global _ACTIVE_CONTEXT
    _ACTIVE_CONTEXT = ctx
