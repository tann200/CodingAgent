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
        # name -> metadata dict (fn, side_effects, description, tags, origin, schema_visibility, __tool_meta__)
        self.tools: Dict[str, Dict[str, Any]] = {}
        self._orchestrator: Optional[Any] = None
        self._explicit_filter: bool = False

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
        tags: Optional[List[str]] = None,
        schema_visibility: str = "primary",
        tool_meta: Optional[Any] = None,
        permission_kind: Optional[Any] = None,
    ) -> None:
        meta_obj = tool_meta if tool_meta is not None else getattr(fn, "__tool_meta__", None)
        entry: Dict[str, Any] = {
            "fn": fn,
            "side_effects": list(side_effects if side_effects is not None else (getattr(meta_obj, "side_effects", None) or [])),
            "description": description or getattr(meta_obj, "description", "") or (fn.__doc__ or "").strip().split("\n")[0],
            "origin": origin,
            "tags": list(tags if tags is not None else (getattr(meta_obj, "tags", None) or [])),
            "schema_visibility": schema_visibility or getattr(meta_obj, "schema_visibility", "primary"),
        }
        if meta_obj is not None:
            entry["__tool_meta__"] = meta_obj
        if permission_kind is not None:
            entry["permission_kind"] = permission_kind
        elif meta_obj is not None and getattr(meta_obj, "permission_kind", None) is not None:
            entry["permission_kind"] = meta_obj.permission_kind
        self.tools[name] = entry

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self.tools.get(name)

    def deregister(self, name: str) -> bool:
        """Remove a tool from the registry."""
        if name in self.tools:
            del self.tools[name]
            return True
        return False

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    def discover(self, module: Any, origin: str = "builtin") -> int:
        """Find all @tool-decorated callables in module and register them."""
        count = 0
        for attr_name in dir(module):
            try:
                obj = getattr(module, attr_name, None)
            except Exception:
                continue
            if callable(obj) and hasattr(obj, "__tool_meta__"):
                meta = getattr(obj, "__tool_meta__")
                name = getattr(meta, "name", None) or getattr(obj, "__name__", "")
                desc = getattr(meta, "description", "") or (obj.__doc__ or "")
                self.register(
                    name,
                    obj,
                    side_effects=getattr(meta, "side_effects", None),
                    description=desc,
                    origin=origin,
                    tags=getattr(meta, "tags", None),
                    schema_visibility=getattr(meta, "schema_visibility", "primary"),
                    tool_meta=meta,
                    permission_kind=getattr(meta, "permission_kind", None),
                )
                count += 1
        return count

    def get_permission_kind(self, name: str) -> Any:
        """Return PermissionKind for *name*, defaulting to PermissionKind.NONE."""
        from src.tools._tool import PermissionKind, TOOL_ATTR

        entry = self.get(name)
        if entry is None:
            return PermissionKind.NONE
        raw = entry.get("permission_kind")
        if isinstance(raw, PermissionKind):
            return raw
        if raw:
            try:
                return PermissionKind(raw)
            except ValueError:
                pass
        meta_obj = entry.get("__tool_meta__")
        if meta_obj is not None and getattr(meta_obj, "permission_kind", None) is not None:
            return meta_obj.permission_kind
        fn = entry.get("fn")
        if fn is not None:
            defn = getattr(fn, TOOL_ATTR, None)
            if defn is not None and getattr(defn, "permission_kind", None) is not None:
                return defn.permission_kind
        return PermissionKind.NONE

    def list(self) -> List[str]:
        return list(self.tools.keys())

    def filter_by_names(self, names: List[str]) -> "ToolRegistry":
        """Return a new ToolRegistry containing only tools whose names are in *names*."""
        filtered = ToolRegistry()
        filtered._orchestrator = getattr(self, "_orchestrator", None)
        filtered._explicit_filter = True
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
            except Exception as exc:
                raise RuntimeError(
                    f"Refusing unguarded tool call {name!r}: orchestrator initialization failed: {exc}"
                ) from exc

        out = orch.execute_tool({"name": name, "arguments": kwargs})
        if isinstance(out, dict) and "result" in out and out.get("ok") is True:
            return out["result"]
        return out

    def get_openai_functions(self, include_aliases: bool = False) -> List[Dict[str, Any]]:
        """Convert registered tools to OpenAI function-calling format.

        Delegates to the authoritative schema generation in
        ``src.tools._registry.ToolRegistry`` so there is a single schema
        implementation across both registry classes.
        """
        try:
            from src.tools._registry import ToolRegistry as _TR

            bridge = _TR()
            bridge._explicit_filter = self._explicit_filter
            for name, meta in self.tools.items():
                fn = meta.get("fn")
                if fn:
                    bridge.register(
                        name=name,
                        fn=fn,
                        description=meta.get("description", ""),
                        side_effects=meta.get("side_effects", []),
                        tags=meta.get("tags", []),
                        origin=meta.get("origin", "builtin"),
                        schema_visibility=meta.get("schema_visibility", "primary"),
                    )
            return bridge.get_openai_functions(include_aliases=include_aliases)
        except Exception:
            return []
