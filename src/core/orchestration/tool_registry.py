"""Tool registry — ARCH-1 / Phase B extraction from orchestrator.py.

Owns:
1. ``ToolRegistry`` — in-memory mapping of tool name → callable + metadata,
   with OpenAI function-schema generation.
2. The per-tool timeout map and ``get_tool_timeout()`` lookup.

Keeping this out of the 4000-line orchestrator means:
- Registries can be constructed and tested without an ``Orchestrator`` instance.
- Timeouts can be tuned without touching the orchestrator.
- Future per-tool metadata can be added incrementally.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Per-tool timeout map (seconds).  0 means no timeout.
# T9: Tool Timeout Protection — values here are the canonical source of truth.
# ---------------------------------------------------------------------------

_TOOL_TIMEOUTS: Dict[str, int] = {
    "bash": 60,
    "run_tests": 120,
    "run_linter": 60,
    "syntax_check": 30,
    "run_js_tests": 120,
    "run_ts_check": 120,
    "run_eslint": 60,
    "search_code": 30,
    "grep": 30,
    "edit_file_atomic": 30,
    "find_symbol": 30,
    "list_files": 10,
    "glob": 10,
}

#: Default timeout used for any tool not listed in ``_TOOL_TIMEOUTS``.
DEFAULT_TOOL_TIMEOUT: int = 30


def get_tool_timeout(tool_name: str) -> int:
    """Return the execution timeout in seconds for *tool_name*.

    Falls back to :data:`DEFAULT_TOOL_TIMEOUT` (30 s) for unknown tools.

    Parameters
    ----------
    tool_name:
        Canonical (alias-resolved) tool name.

    Returns
    -------
    int
        Timeout in seconds.  ``0`` means no timeout (reserved for future use).
    """
    return _TOOL_TIMEOUTS.get(tool_name, DEFAULT_TOOL_TIMEOUT)


# ---------------------------------------------------------------------------
# ToolRegistry — Phase B: moved from orchestrator.py
# ---------------------------------------------------------------------------


class ToolRegistry:
    """In-memory mapping of tool name → callable + metadata.

    Supports registration, lookup, name-based filtering, and serialisation
    to the OpenAI function-calling schema.
    """

    def __init__(self) -> None:
        # name -> metadata dict (fn, side_effects, description)
        self.tools: Dict[str, Dict[str, Any]] = {}
        self._orchestrator: Optional[Any] = None

    def set_orchestrator(self, orchestrator: Any) -> None:
        """Bind an active orchestrator to this registry for guarded tool execution."""
        self._orchestrator = orchestrator

    def get_orchestrator(self) -> Optional[Any]:
        """Return the bound orchestrator, or active orchestrator from ContextVar, or None."""
        if getattr(self, "_orchestrator", None) is not None:
            return self._orchestrator
        try:
            from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

            return _PARENT_ORCHESTRATOR_VAR.get(None)
        except Exception:
            return None

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        side_effects: Optional[List[str]] = None,
        description: str = "",
        origin: str = "builtin",
    ) -> None:
        self.tools[name] = {
            "fn": fn,
            "side_effects": side_effects or [],
            "description": description,
            "origin": origin,
        }

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self.tools.get(name)

    def deregister(self, name: str) -> bool:
        """Remove a tool from the registry."""
        if name in self.tools:
            del self.tools[name]
            return True
        return False

    def list(self) -> List[str]:
        return list(self.tools.keys())

    def filter_by_names(self, names: List[str]) -> "ToolRegistry":
        """Return a new ToolRegistry containing only tools whose names are in *names*."""
        filtered = ToolRegistry()
        filtered._orchestrator = getattr(self, "_orchestrator", None)
        for name in names:
            meta = self.tools.get(name)
            if meta:
                filtered.tools[name] = meta
        return filtered

    def call(self, name: str, **kwargs: Any) -> Any:
        """Call the tool registered as *name* with keyword arguments through guarded execution."""
        entry = self.get(name)
        if entry is None:
            raise KeyError(f"Tool not found: {name!r}")

        orch = self.get_orchestrator()
        if orch is None:
            try:
                from src.core.orchestration.orchestrator import Orchestrator

                wd = kwargs.get("workdir")
                orch = Orchestrator(adapter=None, tool_registry=self, working_dir=str(wd) if wd else None, allow_external_working_dir=True)
                self.set_orchestrator(orch)
            except Exception:
                pass

        if orch is not None:
            out = orch.execute_tool({"name": name, "arguments": kwargs})
            if isinstance(out, dict) and "result" in out and out.get("ok") is True:
                return out["result"]
            return out

        return entry["fn"](**kwargs)

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        """Convert registered tools to OpenAI function-calling format.

        Delegates to the authoritative schema generation in
        ``src.tools._registry.ToolRegistry`` so there is a single schema
        implementation across both registry classes.
        """
        try:
            from src.tools._registry import ToolRegistry as _TR

            bridge = _TR()
            for name, meta in self.tools.items():
                fn = meta.get("fn")
                if fn:
                    bridge.register(
                        name=name,
                        fn=fn,
                        description=meta.get("description", ""),
                        side_effects=meta.get("side_effects", []),
                    )
            return bridge.get_openai_functions()
        except Exception:
            return []
