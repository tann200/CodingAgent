"""
Memory search tool for the coding agent.

Searches agent memory (VectorStore, TASK_STATE.md, compaction checkpoints,
and execution traces) for relevant context. Useful before starting a task
to check whether a similar problem has been solved before.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools.tools_config import agent_context_path
from src.tools._tool import tool

logger = logging.getLogger(__name__)


def _search_vector_store(query: str, workdir: str) -> List[Dict[str, Any]]:
    """Search the VectorStore if available."""
    try:
        from src.core.indexing.vector_store import VectorStore

        vs = VectorStore(workdir)
        raw = vs.search(query)
        results = []
        for item in raw[:5]:
            # LOGIC-1: Use actual similarity score from LanceDB instead of hardcoded 0.8.
            # LanceDB returns _distance (lower = more similar). Convert to a [0,1] score
            # where 1.0 = identical and 0.0 = maximally distant (distance ≥ 2.0 for
            # normalised embeddings).  Clamp to [0, 1] to guard against edge cases.
            distance = item.get("_distance", 1.0) if isinstance(item, dict) else 1.0
            score = max(0.0, min(1.0, 1.0 - distance / 2.0))
            results.append(
                {
                    "source": "vector_store",
                    "excerpt": str(item)[:500],
                    "score": round(score, 4),
                }
            )
        return results
    except Exception:
        return []


def _search_file(path: Path, query: str, source_name: str) -> List[Dict[str, Any]]:
    """Search a single file for query terms and return matching excerpts."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    query_lower = query.lower()
    words = set(re.findall(r"\w+", query_lower))
    if not words:
        return []

    results = []
    for line in text.splitlines():
        line_lower = line.lower()
        matched = [w for w in words if w in line_lower]
        if matched:
            # LOGIC-1: Score proportional to fraction of query words matched,
            # capped at 0.7 (below vector-store scores) to preserve ranking order.
            score = round(min(0.7, 0.3 + 0.4 * len(matched) / len(words)), 4)
            results.append(
                {
                    "source": source_name,
                    "excerpt": line.strip()[:500],
                    "score": score,
                }
            )
    return results[:5]


@tool(tags=["coding", "debug", "planning", "review"])
def memory_search(
    query: str,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Search agent memory for relevant context.

    Searches the vector store, TASK_STATE.md, and execution history for
    entries relevant to the query. Use this before starting a task to
    check whether a similar problem has been solved before.

    Args:
        query: Search terms. Best results with specific technical terms.
        workdir: Working directory (defaults to cwd).

    Returns:
        status, query, results (list of {source, excerpt, score}).
    """
    if not query or not query.strip():
        return {"status": "error", "error": "query must be non-empty"}

    workdir_path = workdir or str(Path.cwd())
    results: List[Dict[str, Any]] = []

    # 1. VectorStore search
    results.extend(_search_vector_store(query, workdir_path))

    # 2. TASK_STATE.md
    ac = agent_context_path(Path(workdir_path))
    results.extend(_search_file(ac / "TASK_STATE.md", query, "TASK_STATE.md"))

    # 3. compaction_checkpoint.md
    results.extend(
        _search_file(ac / "compaction_checkpoint.md", query, "compaction_checkpoint")
    )

    # 4. todo.json
    todo_path = ac / "todo.json"
    if todo_path.exists():
        try:
            todos = json.loads(todo_path.read_text())
            for t in todos:
                desc = t.get("description", "")
                if query.lower() in desc.lower():
                    results.append(
                        {
                            "source": "todo.json",
                            "excerpt": desc[:500],
                            "score": 0.6,
                        }
                    )
        except Exception:
            pass

    # Deduplicate and sort by score
    seen = set()
    unique = []
    for r in results:
        key = (r["source"], r["excerpt"][:80])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique.sort(key=lambda x: x.get("score", 0), reverse=True)

    return {
        "status": "ok",
        "query": query,
        "results": unique[:10],
    }
