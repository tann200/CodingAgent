"""repo_read_tools — read-only repository analysis tools.

Consolidates tools from the following former modules (all still importable
for backward compatibility, but no longer listed in _BUILTIN_MODULES):

- repo_overview_tool.py  → repo_overview()
- repo_summary.py        → helper functions (not @tool-decorated)
- repo_tools.py          → find_files(), search_code(), find_symbol(), find_references()
- repo_analysis_tools.py → analyze_repository()

Grouping convention
-------------------
- repo_read_tools.py  : read-only tools (no filesystem writes, side_effects=[])
- repo_write_tools.py : tools that write to the workspace (side_effects=["write"])
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-export everything from the canonical source modules so that:
#   1. The @tool decorators register correctly (the ToolDefinition is attached
#      to the original function objects).
#   2. Code that imports from repo_read_tools.* continues to work.
# ---------------------------------------------------------------------------

# repo_overview_tool → repo_overview
from src.tools.repo_overview_tool import (  # noqa: F401
    repo_overview,
    _walk_tree,
    _detect_project_type,
    _EXCLUDE_DIRS as _OVERVIEW_EXCLUDE_DIRS,
    _MANIFEST_FILES,
)

# repo_tools → read-only subset
from src.tools.repo_tools import (  # noqa: F401
    find_files,
    search_code,
    find_symbol,
    find_references,
)

# repo_analysis_tools → analyze_repository + private helpers
from src.tools.repo_analysis_tools import (  # noqa: F401
    analyze_repository,
    _analyze_python_files,
    _analyze_python_file,
    _analyze_js_ts_files,
    _analyze_go_files,
    _analyze_rust_files,
)

# repo_summary → utility functions (not @tool decorated, re-exported for callers)
from src.tools.repo_summary import (  # noqa: F401
    detect_framework,
    detect_languages,
    detect_test_framework,
    detect_entrypoints,
    list_modules,
    find_dependency_files,
    generate_repo_summary,
    summarize_repo,
)
