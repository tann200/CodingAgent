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

import inspect
import re
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

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        side_effects: Optional[List[str]] = None,
        description: str = "",
    ) -> None:
        self.tools[name] = {
            "fn": fn,
            "side_effects": side_effects or [],
            "description": description,
        }
        # also register in global registry for other consumers
        try:
            from src.tools.registry import register_tool

            register_tool(
                name, fn, description=description, side_effects=bool(side_effects)
            )
        except Exception:
            pass

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self.tools.get(name)

    def list(self) -> List[str]:
        return list(self.tools.keys())

    def filter_by_names(self, names: List[str]) -> "ToolRegistry":
        """Return a new ToolRegistry containing only tools whose names are in *names*."""
        filtered = ToolRegistry()
        for name in names:
            meta = self.tools.get(name)
            if meta:
                filtered.tools[name] = meta
        return filtered

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        """Convert registered tools to OpenAI function-calling format.

        MC-1 fix: Enable native function-calling API support. Tools are converted
        to the OpenAI /v1/chat/completions 'tools' parameter format with name,
        description, and inferred parameters from function signatures.
        """
        functions = []
        for name, meta in self.tools.items():
            desc = meta.get("description", "")
            fn = meta.get("fn")
            if not fn:
                continue

            # Parse parameters from docstring/signature
            params: Dict[str, Any] = {"type": "object", "properties": {}}
            required: List[str] = []

            # Try to get signature
            try:
                sig = inspect.signature(fn)
                for pname, param in sig.parameters.items():
                    if pname in ("kwargs", "self", "cls"):
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
                    params["properties"][pname] = {"type": ptype}
                    required.append(pname)
            except Exception:
                pass

            # Try to extract params from description
            desc_params = re.findall(r"(\w+)\s*:\s*(\w+)", desc)
            for pname, ptype in desc_params:
                if pname not in params["properties"]:
                    t = "string"
                    if ptype.lower() in ("int", "integer"):
                        t = "integer"
                    elif ptype.lower() in ("float", "number"):
                        t = "number"
                    elif ptype.lower() in ("bool", "boolean"):
                        t = "boolean"
                    elif ptype.lower() in ("list", "array"):
                        t = "array"
                    params["properties"][pname] = {"type": t}
                    if pname not in required:
                        required.append(pname)

            func_def: Dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": params,
                },
            }
            if required:
                func_def["function"]["parameters"]["required"] = required

            functions.append(func_def)

        return functions
