"""repo_write_tools — repository tools that write to the workspace.

Consolidates write-side tools from:
- repo_tools.py → initialize_repo_intelligence()

Grouping convention
-------------------
- repo_read_tools.py  : read-only tools (no filesystem writes, side_effects=[])
- repo_write_tools.py : tools that write to the workspace (side_effects=["write"])
"""

from __future__ import annotations

from typing import Dict, Any

# Lazy imports — degrade gracefully when src.core is not available
try:
    from src.core.indexing.repo_indexer import index_repository
except ImportError:
    index_repository = None

try:
    from src.core.indexing.vector_store import VectorStore
except ImportError:
    VectorStore = None

from src.tools._tool import tool


@tool(side_effects=["write"], tags=["coding"])
def initialize_repo_intelligence(workdir: str) -> Dict[str, Any]:
    """
    Initializes or updates the repository index and vector store.
    """
    if index_repository is None or VectorStore is None:
        return {"status": "error", "error": "src.core.indexing not available"}
    try:
        repo_index = index_repository(workdir)

        vs = VectorStore(workdir)
        vs.index_code(repo_index)

        return {
            "status": "ok",
            "indexed_files": len(repo_index.get("files", [])),
            "indexed_symbols": len(repo_index.get("symbols", [])),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
