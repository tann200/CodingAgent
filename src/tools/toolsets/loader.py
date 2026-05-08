"""Compatibility shim for toolset loading.

Historically ``src.tools.toolsets.loader`` provided a YAML-only loader that
looked for toolset files under ``src/tools/toolsets/`` and ``src/config/toolsets``.
To centralise behaviour and add JSON support plus model-aware format selection
we now provide the canonical implementation at ``src.config.toolsets.loader``.
This module is a thin backward-compatible shim that lazily delegates calls to
the canonical loader so existing imports continue to work.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _get_canonical_loader():
    """Lazily import and return the canonical toolset loader module.

    This avoids importing src.config.toolsets.loader at module import time and
    keeps the legacy module lightweight.
    """
    try:
        import importlib

        return importlib.import_module("src.config.toolsets.loader")
    except Exception:  # pragma: no cover - import errors are covered by tests
        raise


def load_toolset(name: str) -> Optional[Dict[str, Any]]:
    """Load a toolset by name (delegates to src.config.toolsets.loader.load_toolset)."""
    loader = _get_canonical_loader()
    return getattr(loader, "load_toolset")(name)


def load_toolset_for_model(name: str, model: Optional[str]) -> Optional[Dict[str, Any]]:
    """Load a toolset using the canonical model-aware format selection helper."""
    loader = _get_canonical_loader()
    return getattr(loader, "load_toolset_for_model")(name, model)


def get_tools_for_toolset(name: str) -> List[str]:
    loader = _get_canonical_loader()
    return getattr(loader, "get_tools_for_toolset")(name)


def get_toolset_for_role(role: str) -> str:
    loader = _get_canonical_loader()
    return getattr(loader, "get_toolset_for_role")(role)


def get_tools_for_role(role: str) -> List[str]:
    loader = _get_canonical_loader()
    return getattr(loader, "get_tools_for_role")(role)


def list_available_toolsets() -> List[str]:
    loader = _get_canonical_loader()
    return getattr(loader, "list_available_toolsets")()


def get_toolset_description(name: str) -> str:
    loader = _get_canonical_loader()
    return getattr(loader, "get_toolset_description")(name)


def invalidate_cache() -> None:
    loader = _get_canonical_loader()
    return getattr(loader, "clear_cache")()


class ToolsetManager:
    """Delegating ToolsetManager that forwards to canonical implementation."""

    def __init__(self, base_tools: Optional[List[str]] = None) -> None:
        loader = _get_canonical_loader()
        Impl = getattr(loader, "ToolsetManager")
        self._impl = Impl(base_tools=base_tools)

    def select_toolset(self, role: str) -> List[str]:
        return self._impl.select_toolset(role)

    def get_current_toolset(self) -> Optional[str]:
        return self._impl.get_current_toolset()

    def get_toolset_tools(self, name: str) -> List[str]:
        return self._impl.get_toolset_tools(name)
