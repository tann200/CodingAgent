#!/usr/bin/env python3
"""
Analyze collected timings and session_store diagnostic sidecars under tests/_ci_artifacts.

Produces a JSON report and prints a short human-readable summary.

Usage:
  python3 scripts/analyze_timings.py --artifacts tests/_ci_artifacts --out tests/_ci_artifacts/aggregated_report.json
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--artifacts", default="tests/_ci_artifacts")
    p.add_argument("--out", default="tests/_ci_artifacts/aggregated_report.json")
    return p.parse_args()


def extract_run_from_path(path: str) -> int | None:
    # Expect path like .../run_<n>_agent_context/... or .../run_<n>.log
    m = re.search(r"run_(\d+)", path)
    if not m:
        return None
    return int(m.group(1))


def percentile(sorted_list: list[float], p: float) -> float:
    if not sorted_list:
        return 0.0
    if p <= 0:
        return sorted_list[0]
    if p >= 1:
        return sorted_list[-1]
    k = (len(sorted_list) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_list[int(k)]
    d0 = sorted_list[int(f)] * (c - k)
    d1 = sorted_list[int(c)] * (k - f)
    return d0 + d1


def safe_load_json(path: str):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def main() -> None:
    args = parse_args()
    artifacts = args.artifacts
    out_path = args.out

    manifest_path = os.path.join(artifacts, "manifest.json")
    manifest = safe_load_json(manifest_path) or {}
    runs_manifest = manifest.get("results", [])
    total_runs = len(runs_manifest) if runs_manifest else None

    # Find timings files and sidecar diagnostics
    timings_files = glob.glob(
        os.path.join(artifacts, "run_*_agent_context", "timings.json")
    )
    sidecar_files = glob.glob(
        os.path.join(
            artifacts, "run_*_agent_context", "session_store_write_failure_*.json"
        )
    )

    per_phase_entries = defaultdict(list)
    per_phase_by_run = defaultdict(lambda: defaultdict(list))
    runs_with_timings = set()

    for tf in timings_files:
        data = safe_load_json(tf)
        run = extract_run_from_path(tf)
        if data is None:
            continue
        # timings.json might be a list of entries or a dict with 'timings'
        entries = (
            data
            if isinstance(data, list)
            else data.get("timings")
            if isinstance(data, dict)
            else None
        )
        if not entries:
            continue
        runs_with_timings.add(run)
        for e in entries:
            phase = e.get("phase") if isinstance(e, dict) else None
            if not phase:
                continue
            per_phase_entries[phase].append(e)
            per_phase_by_run[run][phase].append(e)

    # Aggregate session_store_write_exhausted stats
    exhausted_elapsed = []
    exhausted_by_run = defaultdict(list)
    for run, phases in per_phase_by_run.items():
        for e in phases.get("session_store_write_exhausted", []):
            elapsed = e.get("elapsed") if isinstance(e, dict) else None
            if isinstance(elapsed, (int, float)):
                exhausted_elapsed.append(float(elapsed))
                exhausted_by_run[run].append(float(elapsed))

    exhausted_elapsed_sorted = sorted(exhausted_elapsed)

    # Sidecar parsing
    sidecars_by_run = defaultdict(list)
    attempts_counter = defaultdict(int)
    for sf in sidecar_files:
        data = safe_load_json(sf)
        run = extract_run_from_path(sf)
        if data is None:
            continue
        sidecars_by_run[run].append(data)
        attempts = data.get("attempts") if isinstance(data, dict) else None
        if isinstance(attempts, int):
            attempts_counter[attempts] += 1

    # Build report
    report = {
        "total_runs_manifest": total_runs,
        "timings_files_count": len(timings_files),
        "sidecar_files_count": len(sidecar_files),
        "runs_with_timings_count": len(runs_with_timings),
        "runs_with_sidecars_count": len(sidecars_by_run),
        "session_store_exhausted": {
            "total_events": len(exhausted_elapsed),
            "unique_runs": len(exhausted_by_run),
            "percent_runs_affected": (len(exhausted_by_run) / total_runs * 100)
            if total_runs
            else None,
            "elapsed_stats": {
                "count": len(exhausted_elapsed),
                "min": min(exhausted_elapsed_sorted)
                if exhausted_elapsed_sorted
                else None,
                "max": max(exhausted_elapsed_sorted)
                if exhausted_elapsed_sorted
                else None,
                "mean": statistics.mean(exhausted_elapsed_sorted)
                if exhausted_elapsed_sorted
                else None,
                "median": statistics.median(exhausted_elapsed_sorted)
                if exhausted_elapsed_sorted
                else None,
                "p95": percentile(exhausted_elapsed_sorted, 0.95)
                if exhausted_elapsed_sorted
                else None,
            },
        },
        "attempts_distribution": dict(attempts_counter),
        "phase_counts": {
            phase: len(entries) for phase, entries in per_phase_entries.items()
        },
        "top_runs_by_sidecars": [],
        "top_runs_by_exhausted_elapsed": [],
    }

    # Top runs by number of sidecars
    top_sidecar_runs = sorted(
        sidecars_by_run.items(), key=lambda kv: len(kv[1]), reverse=True
    )[:10]
    report["top_runs_by_sidecars"] = [
        {"run": run, "count": len(items)} for run, items in top_sidecar_runs
    ]

    # Top runs by max exhausted elapsed
    exhausted_run_stats = []
    for run, elist in exhausted_by_run.items():
        if not elist:
            continue
        exhausted_run_stats.append((run, max(elist), len(elist)))
    exhausted_run_stats_sorted = sorted(
        exhausted_run_stats, key=lambda t: t[1], reverse=True
    )[:10]
    report["top_runs_by_exhausted_elapsed"] = [
        {"run": run, "max_elapsed": mx, "events": cnt}
        for run, mx, cnt in exhausted_run_stats_sorted
    ]

    # Per-phase elapsed stats (for phases that include elapsed)
    per_phase_stats = {}
    for phase, entries in per_phase_entries.items():
        elist = [
            float(e.get("elapsed"))
            for e in entries
            if isinstance(e.get("elapsed"), (int, float))
        ]
        elist_sorted = sorted(elist)
        if not elist_sorted:
            continue
        per_phase_stats[phase] = {
            "count": len(elist_sorted),
            "min": elist_sorted[0],
            "max": elist_sorted[-1],
            "mean": statistics.mean(elist_sorted),
            "median": statistics.median(elist_sorted),
            "p95": percentile(elist_sorted, 0.95),
        }
    report["per_phase_elapsed_stats"] = per_phase_stats

    # Write report
    try:
        with open(out_path, "w") as outf:
            json.dump(report, outf, indent=2)
    except Exception as e:
        print(f"Failed to write report to {out_path}: {e}", file=sys.stderr)

    # Print short human-readable summary
    print("Aggregated Timings Report")
    print("-------------------------")
    print(f"Total runs (manifest): {total_runs}")
    print(f"Timings files found: {len(timings_files)}")
    print(f"Session-store sidecar files found: {len(sidecar_files)}")
    print(f"Runs with any exhausted write events: {len(exhausted_by_run)}")
    if exhausted_elapsed_sorted:
        print(
            f"Exhausted write elapsed (mean/median/p95/max): {report['session_store_exhausted']['elapsed_stats']['mean']:.3f} / {report['session_store_exhausted']['elapsed_stats']['median']:.3f} / {report['session_store_exhausted']['elapsed_stats']['p95']:.3f} / {report['session_store_exhausted']['elapsed_stats']['max']:.3f}"
        )
    else:
        print("No exhausted write elapsed entries found in timings.json files.")

    print("Top runs by exhausted elapsed (max):")
    for item in report["top_runs_by_exhausted_elapsed"]:
        print(
            f"- run {item['run']}: max_elapsed={item['max_elapsed']:.3f} ({item['events']} events)"
        )

    print("Attempts distribution (from sidecars):")
    for attempts, cnt in sorted(report["attempts_distribution"].items()):
        print(f"- attempts={attempts}: {cnt} sidecars")

    print("Report JSON written to:", out_path)


if __name__ == "__main__":
    main()
