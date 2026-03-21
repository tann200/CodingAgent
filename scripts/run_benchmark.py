#!/usr/bin/env python3
"""
CodingAgent benchmark runner.

Usage:
    python scripts/run_benchmark.py                        # run all scenarios
    python scripts/run_benchmark.py --filter easy          # only easy scenarios
    python scripts/run_benchmark.py --filter category=testing
    python scripts/run_benchmark.py --scenario hello_world_function
    python scripts/run_benchmark.py --compare results/baseline.json

Outputs:
    - Live progress to stdout
    - JSON results saved to results/benchmark_<timestamp>.json
    - Optional regression report when --compare is used
"""

from __future__ import annotations

import argparse
import json
import sys
import os
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path
_project_root = Path(__file__).parent.parent
sys.path.insert(0, str(_project_root))


def _build_agent_factory(working_dir: str):
    """Return a factory that creates a fresh agent for each scenario."""
    def factory():
        try:
            from src.core.orchestration.orchestrator import Orchestrator
            return Orchestrator(working_dir=working_dir)
        except Exception as e:
            raise RuntimeError(f"Could not create Orchestrator: {e}") from e
    return factory


def _load_scenarios(scenarios_path: Path, filter_str: str | None, name: str | None):
    """Load and filter scenarios from the benchmark suite JSON."""
    from src.core.evaluation.scenario_evaluator import Scenario

    with open(scenarios_path) as f:
        data = json.load(f)

    all_scenarios = []
    for item in data.get("scenarios", []):
        all_scenarios.append(Scenario(
            name=item["name"],
            description=item.get("description", ""),
            task=item["task"],
            setup_files=item.get("setup_files", {}),
            expected_files=item.get("expected_files", {}),
            test_command=item.get("test_command"),
            cleanup_command=item.get("cleanup_command"),
        ))

    if name:
        all_scenarios = [s for s in all_scenarios if s.name == name]

    if filter_str:
        if "=" in filter_str:
            key, val = filter_str.split("=", 1)
            filtered = []
            for item, sc in zip(data["scenarios"], all_scenarios):
                if str(item.get(key, "")) == val:
                    filtered.append(sc)
            all_scenarios = filtered
        else:
            # filter by difficulty
            filtered = []
            for item, sc in zip(data["scenarios"], all_scenarios):
                if item.get("difficulty") == filter_str or filter_str in item.get("tags", []):
                    filtered.append(sc)
            all_scenarios = filtered

    return all_scenarios


def _compare_results(current: dict, baseline_path: Path) -> list[str]:
    """Compare current results against a baseline, return regression lines."""
    with open(baseline_path) as f:
        baseline = json.load(f)

    baseline_by_name = {r["scenario_name"]: r for r in baseline.get("results", [])}
    current_by_name = {r["scenario_name"]: r for r in current.get("results", [])}

    regressions = []
    for name, cur in current_by_name.items():
        base = baseline_by_name.get(name)
        if not base:
            continue
        if base["status"] == "pass" and cur["status"] != "pass":
            regressions.append(
                f"  REGRESSION  {name}: was pass, now {cur['status']}"
            )
        elif base["status"] != "pass" and cur["status"] == "pass":
            regressions.append(
                f"  IMPROVEMENT {name}: was {base['status']}, now pass"
            )
    return regressions


def main():
    parser = argparse.ArgumentParser(
        description="Run CodingAgent benchmark scenarios",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--scenarios-file",
        default=str(_project_root / "benchmarks" / "scenarios.json"),
        help="Path to scenarios JSON file (default: benchmarks/scenarios.json)",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Filter scenarios by difficulty (easy/medium/hard), tag, or key=value",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="Run a single named scenario",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_project_root / "results"),
        help="Directory for JSON result files (default: results/)",
    )
    parser.add_argument(
        "--compare",
        default=None,
        metavar="BASELINE_JSON",
        help="Compare results against a baseline JSON file for regression detection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching scenarios without running them",
    )
    args = parser.parse_args()

    scenarios_path = Path(args.scenarios_file)
    if not scenarios_path.exists():
        print(f"ERROR: scenarios file not found: {scenarios_path}", file=sys.stderr)
        sys.exit(1)

    scenarios = _load_scenarios(scenarios_path, args.filter, args.scenario)
    if not scenarios:
        print("No scenarios matched the given filter.", file=sys.stderr)
        sys.exit(1)

    print(f"CodingAgent Benchmark Runner")
    print(f"Scenarios file : {scenarios_path}")
    print(f"Matched        : {len(scenarios)} scenario(s)")
    print()

    if args.dry_run:
        for s in scenarios:
            print(f"  {s.name}: {s.description}")
        sys.exit(0)

    from src.core.evaluation.scenario_evaluator import ScenarioEvaluator
    import tempfile

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Each scenario gets its own temp workdir
    all_results = []
    for i, scenario in enumerate(scenarios, 1):
        with tempfile.TemporaryDirectory(prefix=f"benchmark_{scenario.name}_") as tmpdir:
            print(f"[{i}/{len(scenarios)}] Running: {scenario.name} ...", end=" ", flush=True)
            evaluator = ScenarioEvaluator(workdir=tmpdir)
            evaluator.add_scenario(scenario)
            agent_factory = _build_agent_factory(tmpdir)
            results = evaluator.run_evaluation(agent_factory)
            r = results[0]
            all_results.append({
                "scenario_name": r.scenario_name,
                "status": r.status,
                "duration_seconds": round(r.duration_seconds, 2),
                "error": r.error,
                "verification_output": r.verification_output,
            })
            status_icon = "✅" if r.status == "pass" else ("❌" if r.status == "fail" else "⚠️ ")
            print(f"{status_icon} {r.status} ({r.duration_seconds:.1f}s)")

    # Summary
    passed = sum(1 for r in all_results if r["status"] == "pass")
    failed = sum(1 for r in all_results if r["status"] == "fail")
    errors = sum(1 for r in all_results if r["status"] == "error")
    total = len(all_results)
    pass_rate = (passed / total * 100) if total else 0
    total_duration = sum(r["duration_seconds"] for r in all_results)

    print()
    print("=" * 50)
    print(f"Results: {passed}/{total} passed ({pass_rate:.0f}%)")
    print(f"  ✅ Passed : {passed}")
    print(f"  ❌ Failed : {failed}")
    print(f"  ⚠️  Errors : {errors}")
    print(f"  ⏱  Total  : {total_duration:.1f}s")
    print("=" * 50)

    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_file = output_dir / f"benchmark_{timestamp}.json"
    output = {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "pass_rate": round(pass_rate / 100, 4),
            "total_duration_seconds": round(total_duration, 2),
        },
        "results": all_results,
    }
    result_file.write_text(json.dumps(output, indent=2))
    print(f"\nResults saved: {result_file}")

    # Regression comparison
    if args.compare:
        baseline_path = Path(args.compare)
        if not baseline_path.exists():
            print(f"WARNING: baseline file not found: {baseline_path}", file=sys.stderr)
        else:
            regressions = _compare_results(output, baseline_path)
            if regressions:
                print("\nRegression report:")
                for line in regressions:
                    print(line)
            else:
                print("\nNo regressions detected vs baseline.")

    sys.exit(0 if failed == 0 and errors == 0 else 1)


if __name__ == "__main__":
    main()
