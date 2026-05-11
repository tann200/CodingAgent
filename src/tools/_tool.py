"""Tool definition and registration decorator.

The ``@tool`` decorator marks a plain function as an agent tool, attaching
metadata (name, side_effects, tags) without changing its behaviour.  The
decorated function is returned unchanged so existing code that calls it
directly continues to work.

Usage::

    from src.tools._tool import tool

    @tool(side_effects=["write"], tags=["coding"])
    def write_file(path: str, content: str, workdir: Path = ...) -> dict:
        ...

    # Or with no arguments (uses function name, empty side_effects/tags):
    @tool
    def read_file(path: str, workdir: Path = ...) -> dict:
        ...

The metadata is stored as ``fn.__tool_meta__`` (a ``ToolDefinition``
instance).  ``ToolRegistry.discover()`` reads this attribute to
auto-register decorated functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional

# Attribute name stored on decorated functions
TOOL_ATTR = "__tool_meta__"


# ---------------------------------------------------------------------------
# PermissionKind — TASK-3
# ---------------------------------------------------------------------------


class PermissionKind(str, Enum):
    """Semantic permission category for a tool call.

    Mirrors claw-code's ``PermissionKind`` enum from ``tools/src/lib.rs``.
    Used by ``PermissionGateway`` (TASK-4) and the ``@tool`` decorator so
    each tool's required privilege is declared at definition time rather than
    inferred from ``side_effects``.

    ``side_effects`` is kept for backward compatibility with code that reads
    it (preflight path-containment check, MODIFYING_TOOLS sets, etc.).
    ``permission_kind`` is the authoritative, semantically richer field.
    """

    READ_FILE = "ReadFile"  # read file contents, list dirs, glob
    WRITE_FILE = "WriteFile"  # create / overwrite / edit files
    EXECUTE_BASH = "ExecuteBash"  # run arbitrary shell commands
    NETWORK = "NetworkFetch"  # fetch URLs, web search
    GIT_READ = "GitRead"  # git status / log / diff (no state change)
    GIT_WRITE = "GitWrite"  # git commit / stash / restore (state change)
    MEMORY = "MemoryWrite"  # update persistent vector memory
    DELEGATE = "Delegate"  # spawn a sub-agent session
    LSP_READ = "LSP"  # LSP queries (diagnostics, refs, hover)
    LSP_WRITE = "LSPWrite"  # LSP rename (modifies files via language server)
    PLAN = "Plan"  # toggle plan mode (meta-control)
    NONE = "None"  # read-only, no side effects


_PERMISSION_KIND_TO_TABLE_KIND: dict[PermissionKind, str] = {
    PermissionKind.READ_FILE: "read",
    PermissionKind.WRITE_FILE: "write",
    PermissionKind.EXECUTE_BASH: "bash",
    PermissionKind.NETWORK: "webfetch",
    PermissionKind.GIT_READ: "read",
    PermissionKind.GIT_WRITE: "write",
    PermissionKind.MEMORY: "write",
    PermissionKind.DELEGATE: "delegate_task",
    PermissionKind.LSP_READ: "read",
    PermissionKind.LSP_WRITE: "edit",
    PermissionKind.PLAN: "plan",
    PermissionKind.NONE: "none",
}


def permission_kind_to_table_kind(permission_kind: PermissionKind) -> str:
    """Map a PermissionKind enum to the permission-table kind string."""
    return _PERMISSION_KIND_TO_TABLE_KIND.get(permission_kind, "none")


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------


@dataclass
class ToolDefinition:
    """All metadata the registry needs for one tool."""

    name: str
    fn: Callable[..., Any]
    description: str = ""
    side_effects: List[str] = field(default_factory=list)
    # Toolset tags — hints about which role-toolsets include this tool.
    # The authoritative membership is the YAML files; tags are for
    # documentation and potential auto-generation.
    tags: List[str] = field(default_factory=list)
    # TASK-3: explicit semantic permission category (supercedes side_effects
    # for permission-policy evaluation).
    permission_kind: PermissionKind = PermissionKind.NONE

    def to_openai_schema(self) -> dict:
        """Return an OpenAI function-calling schema dict for this tool."""
        import inspect
        import re

        params: dict = {"type": "object", "properties": {}}
        required: list[str] = []

        try:
            sig = inspect.signature(self.fn)
            for pname, param in sig.parameters.items():
                # Skip *args, **kwargs (by kind), and internal non-LLM params by name
                if param.kind in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                ):
                    continue
                if pname in ("self", "cls", "workdir"):
                    continue
                ptype = "string"
                if param.annotation != inspect.Parameter.empty:
                    ann = str(param.annotation).lower()
                    if "int" in ann:
                        ptype = "integer"
                    elif "float" in ann or "double" in ann:
                        ptype = "number"
                    elif "bool" in ann:
                        ptype = "boolean"
                    elif "list" in ann or "array" in ann:
                        ptype = "array"
                    elif "dict" in ann or "mapping" in ann:
                        ptype = "object"
                prop: dict = {"type": ptype}
                # Pull per-param description from docstring
                doc = self.fn.__doc__ or ""
                m = re.search(rf"{re.escape(pname)}\s*[:\-]\s*(.+?)(?:\n|$)", doc)
                if m:
                    prop["description"] = m.group(1).strip()
                params["properties"][pname] = prop
                if param.default is inspect.Parameter.empty:
                    required.append(pname)
        except Exception:
            pass

        # Post-process: add dynamic enums for well-known parameters.
        # Example: for the load_skill tool we can enumerate available skill names
        # so function-calling LLMs can present a concrete set of choices.
        if self.name == "load_skill":
            self._populate_skill_enum(params)
        if self.name == "delegate_task":
            self._populate_role_enum(params)
        self._populate_toolset_enum(params)

        schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description
                or (self.fn.__doc__ or "").strip().split("\n")[0],
                "parameters": params,
            },
        }
        if required:
            schema["function"]["parameters"]["required"] = required
        return schema

    def _populate_skill_enum(self, params: dict) -> None:
        """Populate 'name' enum from available skills."""
        try:
            # Import lazily to avoid import-time cycles
            from src.tools.skill_tools import _list_skill_names

            names = _list_skill_names()
            if names and "name" in params["properties"]:
                params["properties"]["name"]["enum"] = names
        except Exception:
            # Fail softly: schema generation should never raise
            pass

    def _populate_role_enum(self, params: dict) -> None:
        """Populate 'role' enum from available subagent roles."""
        try:
            # Lazy import to avoid import-time cycles
            from src.tools.subagent_tools import _build_valid_roles

            roles = sorted(list(_build_valid_roles()))
            if roles and "role" in params["properties"]:
                params["properties"]["role"]["enum"] = roles
        except Exception:
            # Fail softly — schema generation must not raise
            pass

    def _populate_toolset_enum(self, params: dict) -> None:
        """Populate toolset/toolset_name enums from available YAMLs."""
        try:
            for pname in ("toolset", "toolset_name"):
                if pname in params["properties"]:
                    try:
                        from src.config.toolsets.loader import (
                            list_available_toolsets,
                        )

                        tnames = list_available_toolsets()
                        if tnames:
                            params["properties"][pname]["enum"] = tnames
                    except Exception:
                        # Fail softly — do not raise during schema generation
                        pass
        except Exception:
            pass


def tool(
    _fn: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    side_effects: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    description: Optional[str] = None,
    permission_kind: Optional[PermissionKind] = None,  # TASK-3
) -> Any:
    """Decorator that marks a function as an agent tool.

    Can be used with or without arguments::

        @tool
        def my_tool(...): ...

        @tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE)
        def my_tool(...): ...

    Parameters
    ----------
    permission_kind:
        Explicit semantic permission category (TASK-3).  When omitted the
        decorator infers a sensible default from ``side_effects``:
        ``["write"]`` → ``WRITE_FILE``, ``["execute"]`` → ``EXECUTE_BASH``,
        ``["network"]`` → ``NETWORK``, otherwise ``NONE``.
    """

    def _wrap(fn: Callable) -> Callable:
        _name = name or fn.__name__
        _desc = description or (fn.__doc__ or "").strip().split("\n")[0]
        # Infer permission_kind from side_effects when not explicitly set
        _perm = permission_kind
        if _perm is None:
            _se = [s.lower() for s in (side_effects or [])]
            if "execute" in _se:
                _perm = PermissionKind.EXECUTE_BASH
            elif "network" in _se:
                _perm = PermissionKind.NETWORK
            elif "write" in _se:
                _perm = PermissionKind.WRITE_FILE
            else:
                _perm = PermissionKind.NONE
        defn = ToolDefinition(
            name=_name,
            fn=fn,
            description=_desc,
            side_effects=list(side_effects or []),
            tags=list(tags or []),
            permission_kind=_perm,
        )
        setattr(fn, TOOL_ATTR, defn)
        return fn

    # Support both @tool and @tool(...) usage
    if _fn is not None:
        return _wrap(_fn)
    return _wrap


def ok(output: str = "", **kwargs: Any) -> dict:
    """Return a standardised success response.

    Accepts an optional positional *output* (convenience for callers that
    pass a bare message string) as well as ``**kwargs``.
    """
    result: dict = {"ok": True, "status": "ok"}
    if output:
        result["output"] = output
    result.update(kwargs)
    return result


def err(msg: str, **kwargs: Any) -> dict:
    """Return a standardised error response."""
    return {"status": "error", "error": msg, **kwargs}


def partial(**kwargs: Any) -> dict:
    """Return a standardised partial (timeout-truncated) response."""
    return {"status": "partial", **kwargs}
