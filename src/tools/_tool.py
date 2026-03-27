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
from typing import Any, Callable, List, Optional

# Attribute name stored on decorated functions
TOOL_ATTR = "__tool_meta__"


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


def tool(
    _fn: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    side_effects: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    description: Optional[str] = None,
) -> Any:
    """Decorator that marks a function as an agent tool.

    Can be used with or without arguments::

        @tool
        def my_tool(...): ...

        @tool(side_effects=["write"], tags=["coding"])
        def my_tool(...): ...
    """

    def _wrap(fn: Callable) -> Callable:
        _name = name or fn.__name__
        _desc = description or (fn.__doc__ or "").strip().split("\n")[0]
        defn = ToolDefinition(
            name=_name,
            fn=fn,
            description=_desc,
            side_effects=list(side_effects or []),
            tags=list(tags or []),
        )
        setattr(fn, TOOL_ATTR, defn)
        return fn

    # Support both @tool and @tool(...) usage
    if _fn is not None:
        return _wrap(_fn)
    return _wrap


def ok(**kwargs: Any) -> dict:
    """Return a standardised success response."""
    return {"status": "ok", **kwargs}


def err(msg: str, **kwargs: Any) -> dict:
    """Return a standardised error response."""
    return {"status": "error", "error": msg, **kwargs}


def partial(**kwargs: Any) -> dict:
    """Return a standardised partial (timeout-truncated) response."""
    return {"status": "partial", **kwargs}
