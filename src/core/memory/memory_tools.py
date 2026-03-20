from typing import Dict, Any, List
from pathlib import Path
import json


def memory_search(query: str, workdir: str) -> Dict[str, Any]:
    """Search TASK_STATE.md and execution_trace.json for the query.
    Returns simple ranked results: exact match lines from TASK_STATE.md first, then trace entries sorted by recency.
    """
    wd = Path(workdir)
    out: Dict[str, Any] = {"query": query, "results": []}
    try:
        task_state = wd / ".agent-context" / "TASK_STATE.md"
        if task_state.exists():
            text = task_state.read_text(encoding="utf-8")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            matches: List[Dict[str, Any]] = []
            for i, line in enumerate(lines):
                if query.lower() in line.lower():
                    matches.append(
                        {"source": "TASK_STATE.md", "line": line, "line_no": i + 1}
                    )
            if matches:
                out["results"].extend(matches)
        trace_path = wd / ".agent-context" / "execution_trace.json"
        if trace_path.exists():
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            rev = list(reversed(trace))
            for entry in rev:
                if query.lower() in json.dumps(entry).lower():
                    out["results"].append({"source": "execution_trace", "entry": entry})
        out["status"] = "ok"
        return out
    except Exception as e:
        return {"status": "error", "error": str(e)}
