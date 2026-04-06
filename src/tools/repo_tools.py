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

from pathlib import Path
from src.tools._path_utils import safe_resolve as _safe_resolve
from src.tools.tools_config import agent_context_path
from src.tools._tool import tool

# TASK-17: Repo-scale glob limit — higher than the file_tools.glob limit (500)
# because repo-wide searches legitimately return tens of thousands of paths.
MAX_GLOB_RESULTS = 10_000


@tool(tags=["coding"])
def find_files(pattern: str, workdir: str) -> Dict[str, Any]:
    """Find files in the repository matching a glob pattern.

    Supports ** for recursive matching.  Returns up to MAX_GLOB_RESULTS (10 000)
    matches.  When truncated, the result includes ``truncated: True`` and
    ``total_found`` so callers know how many results were omitted.

    Returns::

        {"status": "ok", "files": [...], "truncated": bool, "total_found": int}
    """
    try:
        base = Path(workdir).resolve()
        # Reject patterns that would escape the working directory
        if ".." in pattern:
            return {
                "status": "error",
                "error": (
                    "Pattern must not contain '..'. "
                    "Path traversal outside the working directory is not allowed."
                ),
            }
        if "**" in pattern:
            raw = base.glob(pattern)
        else:
            raw = base.rglob(pattern)

        files: list[str] = []
        for p in raw:
            if not p.is_file():
                continue
            try:
                rel = str(p.resolve().relative_to(base))
                files.append(rel)
            except ValueError:
                continue  # resolved outside base — skip

        total_found = len(files)
        truncated = total_found > MAX_GLOB_RESULTS
        files = sorted(files)[:MAX_GLOB_RESULTS]
        return {
            "status": "ok",
            "files": files,
            "truncated": truncated,
            "total_found": total_found,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


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


@tool(tags=["coding"])
def search_code(query: str, workdir: str) -> Dict[str, Any]:
    """
    Performs a semantic search over the codebase.
    """
    if VectorStore is None:
        return {"status": "error", "error": "src.core.indexing not available"}
    try:
        vs = VectorStore(workdir)
        results = vs.search(query)
        return {"status": "ok", "results": results}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding"])
def find_symbol(name: str, workdir: str) -> Dict[str, Any]:
    """
    Finds a symbol (class or function) by its exact name.
    """
    import json

    index_path = agent_context_path(Path(workdir)) / "repo_index.json"
    if not index_path.exists():
        return {
            "status": "error",
            "error": "Repo index not found. Run initialize_repo_intelligence first.",
        }

    with open(index_path, "r", encoding="utf-8") as f:
        repo_index = json.load(f)

    results = [s for s in repo_index["symbols"] if s["symbol_name"] == name]
    return {"status": "ok", "results": results}


@tool(tags=["coding"])
def find_references(name: str, workdir: str) -> Dict[str, Any]:
    """
    Find references to a symbol by scanning indexed files for occurrences of the symbol name.
    Uses word-boundary matching to avoid false positives (e.g. 'run' won't match 'running').
    Returns per-match line numbers and snippets.
    """
    import json
    import re

    try:
        base = Path(workdir)
        index_path = agent_context_path(base) / "repo_index.json"
        if not index_path.exists():
            return {
                "status": "error",
                "error": "Repo index not found. Run initialize_repo_intelligence first.",
            }
        with open(index_path, "r", encoding="utf-8") as f:
            repo_index = json.load(f)
        files = [f.get("path") for f in repo_index.get("files", [])]
        refs = []
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        for rel in files:
            try:
                p = _safe_resolve(str(rel), base)
            except PermissionError:
                continue
            try:
                text = p.read_text(encoding="utf-8")
                lines = text.splitlines()
                for line_num, line in enumerate(lines, 1):
                    for m in pattern.finditer(line):
                        col = m.start() + 1
                        refs.append(
                            {
                                "file": str(rel),
                                "line": line_num,
                                "col": col,
                                "snippet": line.strip(),
                            }
                        )
            except Exception:
                continue
        return {"status": "ok", "results": refs}
    except Exception as e:
        return {"status": "error", "error": str(e)}
