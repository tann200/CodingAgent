#!/usr/bin/env python3
"""Run a stress test for the lock fallback implementation.

Spawns multiple worker processes (tests/utils/lock_stress_worker.py) that
acquire/release the fallback lock repeatedly and print JSON metrics. This
runner aggregates those metrics and checks for leftover temp files.

Usage:
  python tests/utils/lock_stress_runner.py [--processes N] [--iters N]

Defaults: processes=6, iters=1000
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict


def run(processes: int, iters: int, workdir: str = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    worker = Path(__file__).resolve().parent / "lock_stress_worker.py"

    if workdir is None:
        workdir = tempfile.mkdtemp(prefix="lock_stress_")
    workdir = str(Path(workdir).resolve())
    print(f"Using workdir={workdir}")

    procs = []
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)

    for i in range(processes):
        cmd = [str(os.sys.executable), str(worker), workdir, str(iters)]
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        procs.append((i, p))

    aggregated: Dict[str, int] = {}
    any_fail = False
    for idx, p in procs:
        out, err = p.communicate()
        if p.returncode != 0:
            print(f"Worker {idx} failed: returncode={p.returncode}")
            print(err.decode())
            any_fail = True
            continue
        s = out.decode().strip()
        try:
            data = json.loads(s)
            metrics = data.get("metrics", {})
            for k, v in metrics.items():
                aggregated[k] = aggregated.get(k, 0) + int(v)
        except Exception:
            print(f"Worker {idx} produced non-JSON output:\n{s}")
            any_fail = True

    # Check for leftover temp files in .agent-context under workdir
    ac = Path(workdir) / ".codingAgent"
    leftover = []
    if ac.exists():
        for p in ac.rglob("*"):
            if (
                ".tmp." in p.name
                or p.suffix.endswith(".tmp")
                or p.name.endswith(".tmp")
            ):
                leftover.append(str(p))

    summary = {
        "workdir": workdir,
        "processes": processes,
        "iterations_per_process": iters,
        "aggregated_metrics": aggregated,
        "leftover_temp_files": leftover,
        "any_worker_failed": any_fail,
    }

    out_path = Path(workdir) / "lock_stress_summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 1 if any_fail or leftover else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--processes", type=int, default=6)
    p.add_argument("--iters", type=int, default=1000)
    p.add_argument("--workdir", type=str, default=None)
    args = p.parse_args()

    rc = run(args.processes, args.iters, args.workdir)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
