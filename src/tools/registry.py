"""Tools registry for the CodingAgent.

Keep a central registry of named tools (function + metadata). Tools are small
functions that the agent can request the system to call (e.g. read_file,
write_file, list_dir). The registry is intentionally tiny and test-friendly.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
import threading

_lock = threading.Lock()
_registry: Dict[str, Dict[str, Any]] = {}


def register_tool(name: str, func: Callable[..., Any], description: str = '', side_effects: bool = False) -> None:
    """Register a tool by name with a description of its signature and purpose."""
    with _lock:
        _registry[name] = {"func": func, "description": description, "side_effects": side_effects}


def unregister_tool(name: str) -> None:
    with _lock:
        _registry.pop(name, None)


def get_tool(name: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _registry.get(name)


def list_tools() -> List[str]:
    with _lock:
        return list(_registry.keys())


def call_tool(name: str, *args, **kwargs) -> Any:
    """Call a registered tool by name; raise KeyError if not found."""
    tool = get_tool(name)
    if not tool or "func" not in tool:
        raise KeyError(f"Tool not found: {name}")
    func = tool["func"]
    return func(*args, **kwargs)


def clear_registry() -> None:
    with _lock:
        _registry.clear()

def get_tool_descriptions() -> str:
    """Return a formatted string of all registered tools and their descriptions."""
    with _lock:
        lines = []
        for name, data in _registry.items():
            lines.append(f"{name}: {data['description']}")
        return "\n".join(lines)
