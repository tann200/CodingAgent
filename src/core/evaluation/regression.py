"""Golden-regression comparison for evaluation runs (audit PHASE-3 item 3.2).

A baseline JSON captures a scenario-by-scenario outcome.  A later run can be
compared against it, flagging scenarios that regressed (flipped from ``pass``
to ``fail``/``error``) so CI can fail on quality regressions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _scenario_outcomes(results: list) -> Dict[str, Dict[str, Any]]:
    """Map each result to a compact per-scenario outcome record."""
    outcomes: Dict[str, Dict[str, Any]] = {}
    for r in results:
        name = _field(r, "scenario_name")
        rec: Dict[str, Any] = {
            "status": _field(r, "status"),
            "duration_seconds": _field(r, "duration_seconds"),
        }
        prompt_tokens = _field(r, "prompt_tokens")
        completion_tokens = _field(r, "completion_tokens")
        tokens_used = _field(r, "tokens_used")
        if tokens_used is None and (prompt_tokens is not None or completion_tokens is not None):
            tokens_used = int(prompt_tokens or 0) + int(completion_tokens or 0)
        if prompt_tokens is not None:
            rec["prompt_tokens"] = int(prompt_tokens)
        if completion_tokens is not None:
            rec["completion_tokens"] = int(completion_tokens)
        if tokens_used is not None:
            rec["tokens_used"] = int(tokens_used)
        tool_calls = _field(r, "tool_calls")
        if tool_calls is None:
            # ScenarioResult stores total_tool_calls or tool_call_count in some paths
            tool_calls = _field(r, "total_tool_calls")
        if tool_calls is not None:
            rec["tool_calls"] = int(tool_calls)
        malformed = _field(r, "malformed_tool_calls")
        if malformed is not None:
            rec["malformed_tool_calls"] = int(malformed)
        outcomes[str(name)] = rec
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
        "got_regressions": bool(regressed) or bool(missing),
    }


def is_regression(comparison: Dict[str, Any]) -> bool:
    """True when the comparison contains at least one regression."""
    return bool(comparison.get("got_regressions"))


# ---------------------------------------------------------------------------
# Benchmark Diff Gate (TASK-28)
# ---------------------------------------------------------------------------

EXIT_IMPROVED = 0
EXIT_REGRESSED = 1
EXIT_NOISE = 2
EXIT_ERROR = 3


@dataclass(frozen=True)
class DiffVerdict:
    """Structured verdict comparing a baseline run against a candidate run."""

    verdict: str  # "improved" | "regressed" | "noise"
    exit_code: int  # 0 improved, 1 regressed, 2 noise
    threshold: float
    baseline_total: int
    baseline_passed: int
    baseline_pass_rate: float
    candidate_total: int
    candidate_passed: int
    candidate_pass_rate: float
    pass_rate_delta: float
    hard_regressions: List[str]
    improvements: List[str]
    unchanged: List[str]
    new_scenarios: List[str]
    missing_scenarios: List[str]
    duration_delta_seconds: Optional[float]
    tokens_delta: Optional[int]
    tool_calls_delta: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def resolve_run_file(path_or_dir: Any) -> Optional[Path]:
    """Resolve a JSON run file path or select the newest ``*.json`` in a directory."""
    p = Path(path_or_dir)
    if not p.exists():
        return None
    if p.is_dir():
        candidates = sorted(
            (c for c in p.glob("*.json") if c.is_file()),
            key=lambda c: c.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None
    return p if p.is_file() else None


def load_run_doc(path_or_dir: Any) -> Optional[Dict[str, Any]]:
    """Load either a ``baseline-save`` JSON (``scenarios`` dict) or ``run --output`` JSON (``results`` list)."""
    resolved = resolve_run_file(path_or_dir)
    if resolved is None:
        return None
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("scenarios"), dict):
        return data
    if isinstance(data.get("results"), list):
        return {
            "format_version": 1,
            "created_at": data.get("created_at"),
            "summary": data.get("summary") or {},
            "metadata": data.get("request") or {},
            "scenarios": _scenario_outcomes(data["results"]),
        }
    return None


def _sum_optional_metric(scenarios: Dict[str, Dict[str, Any]], key: str) -> Optional[float]:
    values: List[float] = []
    for entry in scenarios.values():
        val = entry.get(key)
        if isinstance(val, (int, float)):
            values.append(float(val))
    return sum(values) if values else None


def diff_runs(
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    threshold: float = 0.02,
) -> DiffVerdict:
    """Compare two benchmark run documents and emit a deterministic CI verdict.

    Exit codes:
      - ``0`` (``improved``): zero hard regressions and ``pass_rate_delta >= threshold``
      - ``1`` (``regressed``): any hard regression (``pass`` -> ``fail``/``error``)
        or ``pass_rate_delta <= -threshold``
      - ``2`` (``noise``): zero hard regressions and ``|pass_rate_delta| < threshold``
    """
    base_map: Dict[str, Dict[str, Any]] = baseline.get("scenarios") or {}
    cand_map: Dict[str, Dict[str, Any]] = candidate.get("scenarios") or {}

    base_total = len(base_map)
    cand_total = len(cand_map)
    base_passed = sum(1 for v in base_map.values() if v.get("status") == "pass")
    cand_passed = sum(1 for v in cand_map.values() if v.get("status") == "pass")

    base_rate = (base_passed / base_total) if base_total > 0 else 0.0
    cand_rate = (cand_passed / cand_total) if cand_total > 0 else 0.0
    delta = cand_rate - base_rate

    hard_regressions: List[str] = []
    improvements: List[str] = []
    unchanged: List[str] = []
    new_scenarios: List[str] = []

    for name, cur in cand_map.items():
        prev = base_map.get(name)
        if prev is None:
            new_scenarios.append(name)
            continue
        prev_st = prev.get("status")
        cur_st = cur.get("status")
        if prev_st == "pass" and cur_st != "pass":
            hard_regressions.append(name)
        elif prev_st != "pass" and cur_st == "pass":
            improvements.append(name)
        else:
            unchanged.append(name)

    missing_scenarios = sorted(name for name in base_map if name not in cand_map)
    hard_regressions.sort()
    improvements.sort()
    unchanged.sort()
    new_scenarios.sort()

    # Optional deltas (computed across shared metrics when present in both runs)
    base_dur = _sum_optional_metric(base_map, "duration_seconds")
    cand_dur = _sum_optional_metric(cand_map, "duration_seconds")
    dur_delta = round(cand_dur - base_dur, 3) if (base_dur is not None and cand_dur is not None) else None

    base_tok = _sum_optional_metric(base_map, "tokens_used")
    cand_tok = _sum_optional_metric(cand_map, "tokens_used")
    if base_tok is None:
        s = baseline.get("summary") or {}
        if isinstance(s.get("total_prompt_tokens"), (int, float)) or isinstance(s.get("total_completion_tokens"), (int, float)):
            base_tok = float(s.get("total_prompt_tokens", 0) or 0) + float(s.get("total_completion_tokens", 0) or 0)
    if cand_tok is None:
        s = candidate.get("summary") or {}
        if isinstance(s.get("total_prompt_tokens"), (int, float)) or isinstance(s.get("total_completion_tokens"), (int, float)):
            cand_tok = float(s.get("total_prompt_tokens", 0) or 0) + float(s.get("total_completion_tokens", 0) or 0)
    tok_delta = int(cand_tok - base_tok) if (base_tok is not None and cand_tok is not None) else None

    base_tc = _sum_optional_metric(base_map, "tool_calls")
    cand_tc = _sum_optional_metric(cand_map, "tool_calls")
    tc_delta = int(cand_tc - base_tc) if (base_tc is not None and cand_tc is not None) else None

    if hard_regressions or delta <= -threshold:
        verdict = "regressed"
        exit_code = EXIT_REGRESSED
    elif delta >= threshold:
        verdict = "improved"
        exit_code = EXIT_IMPROVED
    else:
        verdict = "noise"
        exit_code = EXIT_NOISE

    return DiffVerdict(
        verdict=verdict,
        exit_code=exit_code,
        threshold=threshold,
        baseline_total=base_total,
        baseline_passed=base_passed,
        baseline_pass_rate=base_rate,
        candidate_total=cand_total,
        candidate_passed=cand_passed,
        candidate_pass_rate=cand_rate,
        pass_rate_delta=delta,
        hard_regressions=hard_regressions,
        improvements=improvements,
        unchanged=unchanged,
        new_scenarios=new_scenarios,
        missing_scenarios=missing_scenarios,
        duration_delta_seconds=dur_delta,
        tokens_delta=tok_delta,
        tool_calls_delta=tc_delta,
    )
