"""repo_write_tools — repository tools that write to the workspace.

Consolidates write-side tools from:
- repo_tools.py → initialize_repo_intelligence()

Grouping convention
-------------------
- repo_read_tools.py  : read-only tools (no filesystem writes, side_effects=[])
- repo_write_tools.py : tools that write to the workspace (side_effects=["write"])
"""

from __future__ import annotations

# Re-export from the canonical source module so the @tool decorator registration
# is preserved on the original function object.
from src.tools.repo_tools import initialize_repo_intelligence  # noqa: F401
