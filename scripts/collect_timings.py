#!/usr/bin/env python3
"""
Run a pytest test repeatedly to collect timings and preserve .agent-context artifacts.

Usage:
  python3 scripts/collect_timings.py --runs 30 --timeout 300

This script:
- Runs the given pytest test specification N times (sequentially).
- Uses a unique --basetemp per run (under /tmp) so .agent-context is written there.
- Saves the combined stdout/stderr from each run to tests/_ci_artifacts/run_<i>.log
- Copies the run's .agent-context (if present) to tests/_ci_artifacts/run_<i>_agent_context/
- Writes a manifest.json into tests/_ci_artifacts with per-run results.

The script stops on the first non-zero pytest exit code (or timeout) and preserves that run's artifacts.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time


def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def find_agent_context_dir(basetemp: str) -> str | None:
    # Look for a subdirectory under basetemp that contains a .agent-context directory
    pattern = os.path.join(basetemp, "*", ".agent-context")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def run_pytest_once(
    test_spec: str, basetemp: str, log_path: str, timeout: int
) -> tuple[int, bool, float]:
    cmd = ["pytest", "-q", test_spec, "-s", f"--basetemp={basetemp}"]
    start = time.time()
    timed_out = False
    rc = -1
    ensure_dir(os.path.dirname(log_path))
    with open(log_path, "wb") as logf:
        try:
            proc = subprocess.run(
                cmd, stdout=logf, stderr=subprocess.STDOUT, timeout=timeout
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired as e:
            # Record that we timed out and write marker to the log
            try:
                logf.write(b"\n\n=== RUN TIMEOUT ===\n")
                logf.write(str(e).encode())
            except Exception:
                pass
            timed_out = True
            rc = -1
    return rc, timed_out, time.time() - start


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument(
        "--start", type=int, default=1, help="starting run index (1-based)"
    )
    parser.add_argument(
        "--timeout", type=int, default=300, help="per-run timeout in seconds"
    )
    parser.add_argument(
        "--test",
        default="tests/integration/test_pipeline_mock.py::test_pm6_fix_syntax_pipeline",
        help="pytest test spec to run",
    )
    parser.add_argument("--artifacts", default="tests/_ci_artifacts")
    args = parser.parse_args()

    # Verify we are in repository root that contains tests/
    if not os.path.isdir("tests"):
        print("ERROR: 'tests' directory not found in cwd", file=sys.stderr)
        sys.exit(2)

    ensure_dir(args.artifacts)

    manifest_path = os.path.join(args.artifacts, "manifest.json")

    # Load existing manifest results if present so we can append/replace entries
    results: list[dict] = []
    existing_started_at = None
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r") as mf:
                data = json.load(mf)
                existing_started_at = data.get("started_at")
                existing_results = data.get("results", [])
                # Build a mapping by run index for easy replacement
                existing_map = {
                    r.get("run"): r
                    for r in existing_results
                    if isinstance(r.get("run"), int)
                }
                results = [existing_map[k] for k in sorted(existing_map.keys())]
        except Exception:
            # ignore parse errors and start fresh
            results = []

    for i in range(args.start, args.start + args.runs):
        ts = int(time.time())
        basetemp = f"/tmp/pytest_run_{ts}_{i}"
        log_path = os.path.join(args.artifacts, f"run_{i}.log")

        print(
            f"Starting run {i}/{args.start + args.runs - 1}: basetemp={basetemp}, log={log_path}"
        )
        sys.stdout.flush()

        rc, timed_out, elapsed = run_pytest_once(
            args.test, basetemp, log_path, args.timeout
        )

        agent_context_src = find_agent_context_dir(basetemp)
        agent_context_dest = os.path.join(args.artifacts, f"run_{i}_agent_context")
        ac_found = False
        if agent_context_src:
            try:
                if os.path.exists(agent_context_dest):
                    shutil.rmtree(agent_context_dest)
                shutil.copytree(agent_context_src, agent_context_dest)
                ac_found = True
            except Exception as e:
                print(f"Warning: failed to copy .agent-context: {e}", file=sys.stderr)
        else:
            print(f"No .agent-context found under {basetemp}", file=sys.stderr)

        entry = {
            "run": i,
            "rc": rc,
            "timed_out": bool(timed_out),
            "elapsed": elapsed,
            "basetemp": basetemp,
            "agent_context_found": ac_found,
            "log_path": log_path,
        }

        # Replace any existing entry for this run
        results_map = {
            r.get("run"): r for r in results if isinstance(r.get("run"), int)
        }
        results_map[i] = entry
        # Rebuild sorted results list
        results = [results_map[k] for k in sorted(results_map.keys())]

        # Persist manifest after each run so partial results are available if interrupted
        try:
            manifest_written = {
                "started_at": existing_started_at or int(time.time()),
                "results": results,
            }
            with open(manifest_path, "w") as mf:
                json.dump(manifest_written, mf, indent=2)
        except Exception as e:
            print(f"Warning: failed to write manifest: {e}", file=sys.stderr)

        if rc != 0:
            print(f"Run {i} exited with rc={rc}. Stopping further runs.")
            break

    print(
        "All runs finished (or stopped on failure). Wrote manifest to:", manifest_path
    )
    sys.exit(0 if all(r.get("rc", 0) == 0 for r in results) else 1)


if __name__ == "__main__":
    main()
