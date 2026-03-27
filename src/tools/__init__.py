"""CodingAgent tools package — portable, drop-in, extensible.

This package exposes the complete agent tool suite through a clean public
API.  It can be used as-is inside the CodingAgent project or dropped into
any other project with zero changes to core code.

Quick start
-----------
::

    from src.tools import build_registry

    # Create a ready-to-use registry (discovers all built-in tools)
    registry = build_registry(working_dir="/path/to/project")

    # Call a tool directly
    result = registry.call("read_file", path="src/main.py")

    # Get the registry to pass to the orchestrator
    openai_tools = registry.get_openai_functions()

Adding custom tools
-------------------
::

    from src.tools import tool, build_registry
    from pathlib import Path

    @tool(side_effects=[], tags=["custom"])
    def my_search(query: str, workdir: Path = Path(".")) -> dict:
        \"\"\"Search my custom index.\"\"\"
        return {"status": "ok", "results": []}

    # Option A: pass the module containing your tools
    import my_tools_module
    registry = build_registry(extra_modules=[my_tools_module])

    # Option B: add to an existing registry
    registry = build_registry()
    registry.discover(my_tools_module)

Configuring the working directory
----------------------------------
::

    from src.tools import configure

    configure(
        default_workdir=Path("/path/to/project"),
        context_dir=".my-agent-state",   # overrides ".agent-context"
    )
    registry = build_registry()          # all tools use the new default

Security customisation
----------------------
::

    from src.tools._security import SAFE_COMMANDS, DANGEROUS_PATTERNS

    SAFE_COMMANDS.add("my-read-only-cli")
    DANGEROUS_PATTERNS.append("drop table")   # domain-specific block
"""
from __future__ import annotations

# --- Core decorator / definition -----------------------------------------
from src.tools._tool import tool, ToolDefinition, TOOL_ATTR  # noqa: F401

# --- Registry ------------------------------------------------------------
from src.tools._registry import ToolRegistry, build_registry  # noqa: F401

# --- Configuration -------------------------------------------------------
from src.tools.tools_config import configure, get_default_workdir  # noqa: F401

# --- Path safety ---------------------------------------------------------
from src.tools._path_utils import safe_resolve  # noqa: F401

# --- Security constants (re-exported for convenience) --------------------
from src.tools._security import (  # noqa: F401
    DANGEROUS_PATTERNS,
    SAFE_COMMANDS,
    TEST_COMPILE_COMMANDS,
    RESTRICTED_COMMANDS,
)

__all__ = [
    # decorator + type
    "tool",
    "ToolDefinition",
    "TOOL_ATTR",
    # registry
    "ToolRegistry",
    "build_registry",
    # configuration
    "configure",
    "get_default_workdir",
    # path safety
    "safe_resolve",
    # security constants
    "DANGEROUS_PATTERNS",
    "SAFE_COMMANDS",
    "TEST_COMPILE_COMMANDS",
    "RESTRICTED_COMMANDS",
]
