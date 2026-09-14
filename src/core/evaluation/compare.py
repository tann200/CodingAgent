"""Model comparison evaluation (audit PHASE-4 item 4.5).

Runs the same scenario suite across multiple agent factories (e.g. one per
provider/model) and produces a comparison report: per-model pass rates,
per-scenario winner(s), ranking, and tie counts.  Works with either
``ScenarioResult`` objects or SWE-bench style result dicts (both expose
``scenario_name`` / ``status``), so model comparison composes with the 3.2
scenario runner and the 3.3 SWE-bench harness.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.core.evaluation import cli
from src.core.evaluation.scenario_evaluator import (
    Scenario,
    ScenarioEvaluator,
    get_default_scenarios,
)

AgentFactory = Callable[[], Any]


def _name(result: Any) -> str:
    value = getattr(result, "scenario_name", None)
    if value is None and isinstance(result, dict):
        value = result.get("scenario_name")
    return str(value)


def _status(result: Any) -> str:
    value = getattr(result, "status", None)
    if value is None and isinstance(result, dict):
        value = result.get("status")
    return str(value or "error")


def run_models(
    factories: Dict[str, AgentFactory],
    scenarios: Optional[List[Scenario]] = None,
    *,
    samples: int = 1,
    workdir: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run the scenario suite against each factory, best-of-``samples``.

    Args:
        factories: label → callable returning a fresh agent per attempt.
        scenarios: scenarios to run (defaults to the 23 built-ins).
        samples: attempts per scenario (pass@k, best-of-n status).
        workdir: optional parent directory for run dirs.

    Returns:
        ``{label: {"results": [...], "summary": {...}}}``.
    """
    scenario_list = list(scenarios) if scenarios is not None else get_default_scenarios()
    base = Path(workdir) / "model_runs" if workdir else None
    runs: Dict[str, Dict[str, Any]] = {}
    for label, factory in factories.items():
        label_dir = (base / label.replace("/", "_")).as_posix() if base else None
        if base:
            (base / label.replace("/", "_")).mkdir(parents=True, exist_ok=True)
        best_results = []
        passatk = []
        evaluator = ScenarioEvaluator(workdir=label_dir)
        for scenario in scenario_list:
            best, stats = cli._run_scenario_best(scenario, factory, samples, label_dir)
            best_results.append(best)
            passatk.append(stats)
        evaluator.cleanup()
        runs[label] = {
            "results": best_results,
            "passatk": passatk,
            "summary": evaluator.get_summary(best_results),
        }
    return runs


def compare_models(runs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Build a comparison table from :func:`run_models` output.

    Returns:
        ``{"scenarios": {name: {statuses, winners, best}}, "pass_rates":
        {label: rate}, "ranking": [labels best-first], "tied_scenarios": n}``.
    """
    scenario_names = sorted(
        {_name(r) for run in runs.values() for r in run["results"]}
    )

    rows: Dict[str, Dict[str, Any]] = {}
    for name in scenario_names:
        statuses: Dict[str, str] = {}
        for label, run in runs.items():
            outcome = next((r for r in run["results"] if _name(r) == name), None)
            if outcome is not None:
                statuses[label] = _status(outcome)
        winners = sorted(label for label, st in statuses.items() if st == "pass")
        rows[name] = {
            "statuses": statuses,
            "winners": winners,
            "best": winners or sorted(statuses),
        }

    pass_rates = {label: run["summary"]["pass_rate"] for label, run in runs.items()}
    ranking = sorted(pass_rates, key=lambda label: (-pass_rates[label], label))
    tied_scenarios = sum(1 for row in rows.values() if len(row["winners"]) > 1)
    return {
        "scenarios": rows,
        "pass_rates": pass_rates,
        "ranking": ranking,
        "tied_scenarios": tied_scenarios,
    }


def save_comparison(
    path: Any,
    runs: Dict[str, Dict[str, Any]],
    comparison: Dict[str, Any],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Persist a comparison run as a JSON report."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format_version": 1,
        "kind": "model-comparison",
        "metadata": metadata or {},
        "comparison": comparison,
        "models": {
            label: {
                "summary": run["summary"],
                "results": [
                    {
                        "scenario_name": _name(r),
                        "status": _status(r),
                        "duration_seconds": getattr(r, "duration_seconds", None)
                        or (r.get("duration_seconds") if isinstance(r, dict) else None),
                    }
                    for r in run["results"]
                ],
            }
            for label, run in runs.items()
        },
    }
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def _print_comparison(comparison: Dict[str, Any], runs: Dict[str, Dict[str, Any]]) -> None:
    print("\nModel comparison (pass rate per scenario, best-of-n):")
    pass_rates = comparison["pass_rates"]
    for label in comparison["ranking"]:
        print(f"  {label:<24s} pass_rate={pass_rates[label]:.1%}")
    print("\nPer-scenario winners:")
    for name, row in sorted(comparison["scenarios"].items()):
        statuses = ", ".join(f"{label}={status}" for label, status in row["statuses"].items())
        winner = ", ".join(row["winners"]) if row["winners"] else "(none)"
        print(f"  {name:<32s} {statuses:<30s} winner={winner}")

    scenarios_count = next(iter(runs.values()))["summary"]["total"]
    tied = comparison["tied_scenarios"]
    print(f"\n  scenarios compared: {scenarios_count}")
    print(f"  tied scenarios: {tied}")