"""Structural Protocol definitions for the orchestration layer.

Avoids circular imports while giving mypy enough structural information to
type-check methods that accept duck-typed collaborators.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CLIContextProtocol(Protocol):
    """Anything that can report whether a tool is blocked."""

    def blocks(self, tool_name: str) -> bool:
        ...
