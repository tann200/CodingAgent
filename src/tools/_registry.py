"""Portable ToolRegistry with auto-discovery and plugin support.

This module provides the single source of truth for tool storage.  It
replaces the previously duplicated ``src/tools/registry.py`` (module-level
dict) and the ``ToolRegistry`` class embedded in ``orchestrator.py``.

Both old consumers continue to work — the orchestrator's ``ToolRegistry``
class is now a thin wrapper that delegates here.

Quick start for external projects::

    from src.tools._registry import ToolRegistry, build_registry

    registry = build_registry(working_dir="/path/to/project")
    result = registry.call("read_file", path="src/main.py")

Adding a custom tool without modifying any core file::

    import my_module          # contains @tool-decorated functions
    registry.discover(my_module)
"""

from __future__ import annotations

import importlib
import inspect
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.tools._tool import TOOL_ATTR, ToolDefinition

logger = logging.getLogger(__name__)

# Modules that make up the built-in tool set.  Order is irrelevant.
_BUILTIN_MODULES = [
    "src.tools.file_tools",
    "src.tools.git_tools",
    "src.tools.verification_tools",
    "src.tools.todo_tools",
    "src.tools.subagent_tools",
    "src.tools.repo_tools",
    "src.tools.repo_analysis_tools",
    "src.tools.patch_tools",
    "src.tools.state_tools",
    "src.tools.system_tools",
    "src.tools.memory_tools",
    "src.tools.interaction_tools",
    "src.tools.guardrails",
    "src.tools.web_tools",
    "src.tools.ast_tools",
    "src.tools.project_tools",
]

# Built-in aliases: alias_name -> canonical_name
_BUILTIN_ALIASES: Dict[str, str] = {
    "fs.read": "read_file",
    "fs.write": "write_file",
    "fs.list": "list_files",
    "get_git_diff": "get_git_diff",  # system_tools name (kept distinct from git_diff)
}


class ToolRegistry:
    """Thread-safe registry of agent tools.

    Supports manual registration (``register()``) and automatic discovery of
    ``@tool``-decorated functions via ``discover(module)``.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        side_effects: Optional[List[str]] = None,
        description: str = "",
        tags: Optional[List[str]] = None,
    ) -> None:
        """Manually register a callable under *name*.

        Parameters
        ----------
        name:
            The tool name the agent will reference.
        fn:
            The callable to invoke.
        side_effects:
            List of side-effect tokens, e.g. ``["write"]`` or ``["execute"]``.
        description:
            Human-readable description used in the OpenAI function schema.
        tags:
            Toolset membership hints (e.g. ``["coding", "review"]``).
        """
        entry: Dict[str, Any] = {
            "fn": fn,
            "side_effects": list(side_effects or []),
            "description": description or (fn.__doc__ or "").strip().split("\n")[0],
            "tags": list(tags or []),
            "name": name,
        }
        with self._lock:
            self._tools[name] = entry
        # Mirror into the legacy module-level registry for backward compat
        try:
            from src.tools.registry import register_tool as _rt

            _rt(name, fn, description=description, side_effects=bool(side_effects))
        except Exception:
            pass

    def register_definition(self, defn: ToolDefinition) -> None:
        """Register from a ``ToolDefinition`` (created by ``@tool``)."""
        self.register(
            name=defn.name,
            fn=defn.fn,
            side_effects=defn.side_effects,
            description=defn.description,
            tags=defn.tags,
        )

    def alias(self, alias_name: str, canonical_name: str) -> None:
        """Register *alias_name* as an alias for an already-registered tool."""
        with self._lock:
            canonical = self._tools.get(canonical_name)
        if canonical is None:
            logger.debug(
                "alias: canonical tool '%s' not found, skipping", canonical_name
            )
            return
        entry = dict(canonical)
        entry["name"] = alias_name
        with self._lock:
            self._tools[alias_name] = entry

    # ------------------------------------------------------------------
    # Auto-discovery
    # ------------------------------------------------------------------

    def discover(self, module: Any) -> int:
        """Find all ``@tool``-decorated callables in *module* and register them.

        Parameters
        ----------
        module:
            An already-imported Python module object.

        Returns
        -------
        int
            Number of tools discovered and registered.
        """
        count = 0
        for attr_name in dir(module):
            try:
                obj = getattr(module, attr_name, None)
            except Exception:
                continue
            if callable(obj) and hasattr(obj, TOOL_ATTR):
                defn: ToolDefinition = getattr(obj, TOOL_ATTR)
                self.register_definition(defn)
                count += 1
        return count

    def discover_module_name(self, module_name: str) -> int:
        """Import *module_name* then call ``discover()`` on it.

        Failures are logged at WARNING level and do not propagate so that a
        missing optional dependency does not prevent the registry from loading.
        """
        try:
            mod = importlib.import_module(module_name)
            return self.discover(mod)
        except ImportError as exc:
            logger.warning(
                "discover_module_name: could not import '%s': %s", module_name, exc
            )
            return 0
        except Exception as exc:
            logger.warning("discover_module_name: error in '%s': %s", module_name, exc)
            return 0

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the tool entry dict or *None* if not found."""
        with self._lock:
            return self._tools.get(name)

    def list(self) -> List[str]:
        """Return all registered tool names."""
        with self._lock:
            return list(self._tools.keys())

    def call(self, name: str, **kwargs: Any) -> Any:
        """Call the tool registered as *name* with keyword arguments.

        Raises
        ------
        KeyError
            If no tool is registered under *name*.
        """
        entry = self.get(name)
        if entry is None:
            raise KeyError(f"Tool not found: {name!r}")
        return entry["fn"](**kwargs)

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        """Return all tools formatted as OpenAI function-calling schemas."""
        with self._lock:
            items = list(self._tools.items())
        result = []
        for name, entry in items:
            fn = entry.get("fn")
            if not fn:
                continue
            defn = getattr(fn, TOOL_ATTR, None)
            if defn is not None:
                result.append(defn.to_openai_schema())
            else:
                # Fallback: build minimal schema from signature
                result.append(_minimal_schema(name, fn, entry.get("description", "")))
        return result

    def filter_by_tags(self, *tags: str) -> "ToolRegistry":
        """Return a new registry containing only tools that match any of *tags*."""
        sub = ToolRegistry()
        with self._lock:
            items = list(self._tools.items())
        for name, entry in items:
            tool_tags = set(entry.get("tags", []))
            if tool_tags & set(tags):
                with sub._lock:
                    sub._tools[name] = entry
        return sub

    def filter_by_names(self, names: List[str]) -> "ToolRegistry":
        """Return a new registry restricted to the given tool *names*."""
        sub = ToolRegistry()
        with self._lock:
            for n in names:
                if n in self._tools:
                    sub._tools[n] = self._tools[n]
        return sub

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._tools


# ---------------------------------------------------------------------------
# Schema helper
# ---------------------------------------------------------------------------


def _minimal_schema(name: str, fn: Callable, description: str) -> dict:
    params: dict = {"type": "object", "properties": {}}
    required: list = []
    try:
        sig = inspect.signature(fn)
        for pname, param in sig.parameters.items():
            if pname in ("kwargs", "self", "cls", "workdir"):
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
            if param.default is inspect.Parameter.empty:
                required.append(pname)
    except Exception:
        pass
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
        },
    }
    if required:
        schema["function"]["parameters"]["required"] = required
    return schema


# ---------------------------------------------------------------------------
# build_registry() — one-call setup for external projects
# ---------------------------------------------------------------------------


def build_registry(
    working_dir: Optional[str] = None,
    extra_modules: Optional[List[Any]] = None,
    include_echo: bool = False,
) -> ToolRegistry:
    """Build and return a fully populated ``ToolRegistry``.

    This is the recommended entry point for projects that want to use the
    CodingAgent tool suite.  It:

    1. Optionally configures the default working directory.
    2. Discovers and registers all built-in tools via ``@tool`` decorators.
    3. Registers built-in aliases (``fs.read``, ``fs.write``, ``fs.list``).
    4. Optionally discovers tools from caller-provided *extra_modules*.

    Parameters
    ----------
    working_dir:
        Absolute path to the project root.  When provided,
        ``tools_config.configure(default_workdir=...)`` is called so that
        tool calls without an explicit ``workdir=`` argument use this path.
    extra_modules:
        Additional Python module objects whose ``@tool``-decorated functions
        should be registered.  Pass your own modules here to add tools
        without touching any core file.
    include_echo:
        Register the test-only ``echo`` tool.  Enabled automatically in
        unit-test environments.

    Returns
    -------
    ToolRegistry
        A ready-to-use registry.

    Example
    -------
    ::

        from src.tools import build_registry

        # Minimal — uses cwd as working directory
        registry = build_registry()

        # With explicit workdir
        registry = build_registry(working_dir="/path/to/project")

        # With custom tools
        import my_tools
        registry = build_registry(extra_modules=[my_tools])
    """
    if working_dir is not None:
        from src.tools.tools_config import configure

        configure(default_workdir=Path(working_dir))

    reg = ToolRegistry()

    # Discover all built-in tool modules
    for mod_name in _BUILTIN_MODULES:
        reg.discover_module_name(mod_name)

    # Register built-in aliases
    for alias, canonical in _BUILTIN_ALIASES.items():
        reg.alias(alias, canonical)

    # 'list_files' is the public name; list_dir is the function name
    reg.alias(
        "list_files", "list_files"
    )  # no-op if already registered via @tool(name=...)

    if include_echo:

        def _echo(text: str, **kwargs: Any) -> dict:  # type: ignore[misc]
            return {"status": "ok", "output": text}

        reg.register(
            "echo", _echo, description="echo(text) -> Return the provided text"
        )

    # Caller-supplied extension modules
    for mod in extra_modules or []:
        try:
            reg.discover(mod)
        except Exception as exc:
            logger.warning("build_registry: discover failed for %r: %s", mod, exc)

    logger.debug("build_registry: registered %d tools", len(reg))
    return reg
