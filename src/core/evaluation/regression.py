"""Golden-regression comparison for evaluation runs (audit PHASE-3 item 3.2).

A baseline JSON captures a scenario-by-scenario outcome.  A later run can be
compared against it, flagging scenarios that regressed (flipped from ``pass``
to ``fail``/``error``) so CI can fail on quality regressions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


def _scenario_outcomes(results: list) -> Dict[str, Dict[str, Any]]:
    """Map each result to a compact per-scenario outcome record."""
    outcomes: Dict[str, Dict[str, Any]] = {}
    for r in results:
        name = getattr(r, "scenario_name", None) or r.get("scenario_name")
        outcomes[str(name)] = {
            "status": getattr(r, "status", None) or r.get("status"),
            "duration_seconds": getattr(r, "duration_seconds", None)
            or r.get("duration_seconds"),
        }
    return outcomes


def save_baseline(
    path: Any,
    results: list,
    summary: Optional[Dict[str, Any]] = None,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Persist an evaluation run as a golden baseline JSON document.

    Args:
        path: Destination path (str or Path, parent dirs are created).
        results: Iterable of ``ScenarioResult`` (or dicts).
        summary: Optional pre-computed summary dict.
        metadata: Optional free-form run metadata (model, provider, date…).

    Returns:
        The resolved ``Path`` that was written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": summary or {},
        "metadata": metadata or {},
        "scenarios": _scenario_outcomes(list(results)),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    Path(tmp).replace(path)
    return path


def load_baseline(path: Any) -> Optional[Dict[str, Any]]:
    """Load a baseline JSON document; ``None`` if missing or corrupt."""
    try:
        path = Path(path)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("scenarios"), dict):
            return None
        return data
    except Exception:
        return None


def compare_baseline(baseline: Dict[str, Any], results: list) -> Dict[str, Any]:
    """Compare a baseline against a fresh run.

    Args:
        baseline: Baseline document from :func:`load_baseline`.
        results: Iterable of ``ScenarioResult`` (or dicts).

    Returns:
        Dict with keys:
        - ``scenarios``: per-name ``{result, previous_status, previous_duration}``
        - ``regressed``: names that flipped ``pass`` → ``fail``/``error``
        - ``improved``: names that flipped ``fail``/``error`` → ``pass``
        - ``unchanged``: names whose status did not change
        - ``new``: names present now but absent from the baseline
        - ``missing``: names in the baseline that did not run
        - ``got_regressions``: bool — True when any regression exists
    """
    previous = baseline.get("scenarios") or {}
    current = _scenario_outcomes(list(results))

    regressed: list = []
    improved: list = []
    unchanged: list = []
    new: list = []
    rows: Dict[str, Dict[str, Any]] = {}
    for name, outcome in current.items():
        prev = previous.get(name)
        rows[name] = {
            "result": outcome["status"],
            "previous_status": prev.get("status") if prev else None,
            "previous_duration": prev.get("duration_seconds") if prev else None,
        }
        if prev is None:
            new.append(name)
            continue
        if prev.get("status") in ("pass", "error") and outcome["status"] == "fail":
            regressed.append(name)
        elif prev.get("status") == "pass" and outcome["status"] == "error":
            regressed.append(name)
        elif prev.get("status") in ("fail", "error") and outcome["status"] == "pass":
            improved.append(name)
        else:
            unchanged.append(name)

    missing = sorted(
        name for name in previous if name not in current
    )
    return {
        "scenarios": rows,
        "regressed": sorted(set(regressed)),
        "improved": sorted(set(improved)),
        "unchanged": sorted(set(unchanged)),
        "new": new,
        "missing": missing,
        "got_regressions": bool(regressed),
    }


def is_regression(comparison: Dict[str, Any]) -> bool:
    """True when the comparison contains at least one regression."""
    return bool(comparison.get("got_regressions"))