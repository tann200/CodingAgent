"""
Memory tools for the coding agent.

Provides:
  - memory_search: searches VectorStore, TASK_STATE.md, compaction checkpoints,
    and execution traces for relevant context.
  - memory_save: persists a note to ~/.coding_agent/memory.md so it is available
    in future sessions (GAP-NEW-4 / GAP-FRONTIER-4).
"""

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools.tools_config import agent_context_path
from src.tools._tool import tool, PermissionKind

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


@tool(tags=["coding", "debug", "planning", "review"], permission_kind=PermissionKind.NONE)
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


# ---------------------------------------------------------------------------
# memory_save — GAP-NEW-4 / GAP-FRONTIER-4
# ---------------------------------------------------------------------------

_MEMORY_FILE = Path.home() / ".coding_agent" / "memory.md"
_MEMORY_MAX_BYTES = 50_000  # ~50 KB; trim oldest entries when exceeded


@tool(tags=["coding", "planning", "review"], permission_kind=PermissionKind.MEMORY)
def memory_save(
    content: str,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a note to long-term memory so it is available in future sessions.

    Use this to record learned preferences, project conventions, recurring
    patterns, or any insight that should survive beyond the current session.
    Notes are stored in ~/.coding_agent/memory.md and injected into future
    sessions automatically.

    Args:
        content: The note to save (1–3 sentences, specific and actionable).
        category: Optional category label (e.g. "convention", "preference",
                  "pattern", "warning"). Defaults to "note".

    Returns:
        status, saved entry.
    """
    if not content or not content.strip():
        return {"status": "error", "error": "content must be non-empty"}

    content = content.strip()
    if len(content) > 1000:
        return {
            "status": "error",
            "error": f"content too long ({len(content)} chars, max 1000). Be concise.",
        }

    cat = (category or "note").strip().lower()
    ts = time.strftime("%Y-%m-%d %H:%M")
    entry = f"- [{cat}] {ts}: {content}\n"

    try:
        _MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Read existing content
        existing = ""
        if _MEMORY_FILE.exists():
            try:
                existing = _MEMORY_FILE.read_text(encoding="utf-8")
            except Exception:
                existing = ""

        # Trim oldest entries if file would exceed size limit
        new_content = existing + entry
        if len(new_content.encode("utf-8")) > _MEMORY_MAX_BYTES:
            lines = new_content.splitlines(keepends=True)
            while lines and len("".join(lines).encode("utf-8")) > _MEMORY_MAX_BYTES:
                lines.pop(0)
            new_content = "".join(lines)

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(
            dir=str(_MEMORY_FILE.parent), suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.replace(tmp_path, str(_MEMORY_FILE))

        return {
            "status": "ok",
            "saved": entry.strip(),
            "memory_file": str(_MEMORY_FILE),
            "total_entries": new_content.count("\n- ["),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
