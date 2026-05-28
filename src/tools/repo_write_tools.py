"""repo_write_tools — repository tools that write to the workspace.

Consolidates write-side tools from:
- repo_tools.py → initialize_repo_intelligence()

Grouping convention
-------------------
- repo_read_tools.py  : read-only tools (no filesystem writes, side_effects=[])
- repo_write_tools.py : tools that write to the workspace (side_effects=["write"])
"""

from __future__ import annotations

from typing import Any, Dict

from src.tools._tool import tool

# Lazy imports — degrade gracefully when src.core is not available
_index_repository: Any = None
try:
    from src.core.indexing.repo_indexer import index_repository as _index_repository  # type: ignore[assignment]
except ImportError:
    pass

_VectorStore: Any = None
try:
    from src.core.indexing.vector_store import VectorStore as _VectorStore  # type: ignore[assignment]
except ImportError:
    pass


@tool(side_effects=["write"], tags=["coding"])
def initialize_repo_intelligence(workdir: str) -> Dict[str, Any]:
    """
    Initializes or updates the repository index and vector store.
    """
    if _index_repository is None or _VectorStore is None:
        return {"status": "error", "error": "src.core.indexing not available"}
    try:
        repo_index = _index_repository(workdir)

        vs = _VectorStore(workdir)
        vs.index_code(repo_index)

        return {
            "status": "ok",
            "indexed_files": len(repo_index.get("files", [])),
            "indexed_symbols": len(repo_index.get("symbols", [])),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
