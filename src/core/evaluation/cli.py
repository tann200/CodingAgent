"""Command-line runner for the evaluation framework (audit PHASE-3 item 3.2).

Usage::

    python -m src.core.evaluation list
    python -m src.core.evaluation run --agent pkg.mod:factory [--samples 3]
    python -m src.core.evaluation run --baseline baseline.json --output report.json
    python -m src.core.evaluation baseline-save --output baseline.json

``--agent`` is a dotted ``module:attribute`` path to a callable returning a
fresh agent-like object for each scenario attempt.  Supported agent surfaces
(matched by :func:`scenario_evaluator._run_agent_for_scenario`): ``run(...)``,
``run_agent_once(...)``, or plain ``__call__``.

Exit codes: 0 success; 1 quality regression detected (when ``--baseline`` is
given); 2 usage/configuration error.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.core.evaluation import regression
from src.core.evaluation.scenario_evaluator import (
    Scenario,
    ScenarioEvaluator,
    get_default_scenarios,
    run_pass_at_k,
)


def default_agent_factory() -> Any:
    """Return a fresh ``Orchestrator`` for a live evaluation run."""
    from src.core.orchestration.orchestrator import Orchestrator

    import os

    workdir = os.environ.get("EVAL_WORKDIR") or None
    return Orchestrator(working_dir=workdir, dry_run=False)


def _resolve_agent_factory(spec: str) -> Callable[[], Any]:
    """Resolve a ``module:attribute`` factory spec (default if empty)."""
    if not spec or spec == "default":
        return default_agent_factory
    if ":" not in spec:
        raise argparse.ArgumentTypeError(
            f"--agent must be 'module:callable', got {spec!r}"
        )
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise argparse.ArgumentTypeError(
            f"cannot import agent module {module_name!r}: {exc}"
        ) from exc
    factory = getattr(module, attr, None)
    if factory is None or not callable(factory):
        raise argparse.ArgumentTypeError(
            f"{module_name}:{attr} is not callable (imported via {module_name!r})"
        )
    return factory


def _select_scenarios(
    scenarios: Optional[List[Scenario]] = None,
    names: Optional[List[str]] = None,
    category: Optional[str] = None,
    difficulty: Optional[str] = None,
) -> List[Scenario]:
    """Filter the default scenario set by name/category/difficulty."""
    scenarios = list(scenarios if scenarios is not None else get_default_scenarios())
    if names:
        wanted = set(names)
        scenarios = [s for s in scenarios if s.name in wanted]
    if category:
        scenarios = [s for s in scenarios if s.category == category]
    if difficulty:
        scenarios = [s for s in scenarios if s.difficulty == difficulty]
    return scenarios


def _run_scenario_best(
    scenario: Scenario,
    agent_factory: Callable[[], Any],
    samples: int,
    workdir: Optional[str],
) -> tuple:
    """Run *scenario* ``samples`` times, returning best + pass@k summary."""
    if samples <= 1:
        evaluator = ScenarioEvaluator(workdir=workdir)
        result = evaluator.run_scenario(scenario, agent_factory)
        evaluator.cleanup()
        return result, None
    out = run_pass_at_k(
        scenario,
        agent_factory,
        n=samples,
        k=1,
        workdir=workdir,
    )
    # Best-of-n candidate result for regression comparison: pass if any pass.
    results = out["results"]
    any_pass = any(r.status == "pass" for r in results)
    first_error = next((r for r in results if r.status == "error"), None)
    best_status = "pass" if any_pass else (first_error.status if first_error else "fail")
    best_result = results[0]
    best_result.status = best_status
    return best_result, out


def _run(
    *,
    agent_factory: Callable[[], Any],
    scenarios: List[Scenario],
    samples: int,
    workdir: Optional[str],
) -> Dict[str, Any]:
    """Execute the selected scenarios and return report data."""
    best_results = []
    passatk = []
    evaluator = ScenarioEvaluator(workdir=workdir)
    for scenario in scenarios:
        best, stats = _run_scenario_best(
            scenario, agent_factory, samples=samples, workdir=workdir
        )
        best_results.append(best)
        passatk.append(stats)
    evaluator.cleanup()
    summary = evaluator.get_summary(best_results)
    return {
        "best_results": best_results,
        "passatk": passatk,
        "summary": summary,
    }


def _print_report(report: Dict[str, Any], samples: int) -> None:
    summary = report["summary"]
    print(f"\nResults ({samples} sample(s) per scenario):")
    print(f"  total   : {summary['total']}")
    print(f"  passed  : {summary['passed']}")
    print(f"  failed  : {summary['failed']}")
    print(f"  errors  : {summary['errors']}")
    print(f"  pass rate: {summary['pass_rate']:.1%}")
    if samples > 1:
        print("  pass@k  :")
        for entry in report["passatk"]:
            if entry:
                print(
                    f"    {entry['scenario']:<32s} pass@k={entry['pass_at_k']:.2f} "
                    f"({entry['c']}/{entry['n']})"
                )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.core.evaluation",
        description="CodingAgent scenario evaluation framework.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List available scenarios")
    list_p.add_argument("--category", default=None)
    list_p.add_argument("--difficulty", default=None)

    run_p = sub.add_parser("run", help="Run scenarios and report results")
    run_p.add_argument("--agent", default="default", help="module:callable factory")
    run_p.add_argument(
        "--scenarios", nargs="*", default=None, help="Scenario names to run"
    )
    run_p.add_argument("--category", default=None)
    run_p.add_argument("--difficulty", default=None)
    run_p.add_argument("--samples", type=int, default=1, help="Attempts per scenario")
    run_p.add_argument("--workdir", default=None, help="Working directory for runs")
    run_p.add_argument("--output", default=None, help="Write JSON report here")
    run_p.add_argument(
        "--baseline", default=None, help="Baseline JSON; exit 1 on regression"
    )

    save_p = sub.add_parser("baseline-save", help="Run and save a golden baseline")
    save_p.add_argument("--agent", default="default", help="module:callable factory")
    save_p.add_argument(
        "--scenarios", nargs="*", default=None, help="Scenario names to run"
    )
    save_p.add_argument("--category", default=None)
    save_p.add_argument("--difficulty", default=None)
    save_p.add_argument("--samples", type=int, default=1)
    save_p.add_argument("--workdir", default=None)
    save_p.add_argument("--output", required=True, help="Baseline JSON path")
    save_p.add_argument("--metadata", nargs="*", default=[], help="k=v run metadata")
    return parser


def _cmd_list(args: argparse.Namespace) -> int:
    scenarios = _select_scenarios(
        category=args.category, difficulty=args.difficulty
    )
    for s in scenarios:
        print(
            f"{s.name:<30s} [{s.category:<20s} {s.difficulty:<8s}] {s.task[:60]}"
        )
    print(f"\n{len(scenarios)} scenarios.")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    factory = _resolve_agent_factory(args.agent)
    scenarios = _select_scenarios(
        names=args.scenarios, category=args.category, difficulty=args.difficulty
    )
    if not scenarios:
        print("No scenarios matched.", file=sys.stderr)
        return 2
    report = _run(
        agent_factory=factory,
        scenarios=scenarios,
        samples=args.samples,
        workdir=args.workdir,
    )
    _print_report(report, args.samples)
    if args.output:
        _write_report(args.output, report, args)
    if args.baseline:
        baseline = regression.load_baseline(args.baseline)
        if baseline is None:
            print(f"Baseline not found or corrupt: {args.baseline}", file=sys.stderr)
            return 2
        comparison = regression.compare_baseline(
            baseline, report["best_results"]
        )
        print("\nBaseline comparison:")
        print(f"  regressed: {comparison['regressed'] or 'none'}")
        print(f"  improved : {comparison['improved'] or 'none'}")
        if comparison["got_regressions"]:
            print("  -> REGRESSION DETECTED (exit 1)")
            return 1
    return 0


def _write_report(path: str, report: Dict[str, Any], args: argparse.Namespace) -> None:
    from datetime import datetime

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "created_at": datetime.now().isoformat(),
        "request": {
            "agent": args.agent,
            "samples": args.samples,
            "scenarios": [r.scenario_name for r in report["best_results"]],
        },
        "summary": report["summary"],
        "results": [
            {
                "scenario_name": r.scenario_name,
                "status": r.status,
                "duration_seconds": r.duration_seconds,
                "error": r.error,
                "verification_output": r.verification_output,
            }
            for r in report["best_results"]
        ],
    }
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report written to {out}")


def _cmd_baseline_save(args: argparse.Namespace) -> int:
    factory = _resolve_agent_factory(args.agent)
    scenarios = _select_scenarios(
        names=args.scenarios, category=args.category, difficulty=args.difficulty
    )
    if not scenarios:
        print("No scenarios matched.", file=sys.stderr)
        return 2
    report = _run(
        agent_factory=factory,
        scenarios=scenarios,
        samples=args.samples,
        workdir=args.workdir,
    )
    _print_report(report, args.samples)
    metadata: Dict[str, Any] = {}
    for kv in args.metadata:
        if "=" in kv:
            k, _, v = kv.partition("=")
            metadata[k.strip()] = v.strip()
    path = regression.save_baseline(
        args.output,
        report["best_results"],
        summary=report["summary"],
        metadata=metadata,
    )
    print(f"Baseline saved to {path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "baseline-save":
        return _cmd_baseline_save(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())