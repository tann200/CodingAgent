"""Background helper to refresh TASK_STATE.md and file_summaries.json periodically.

Usage:
    python scripts/refresh_summaries.py /path/to/workdir --interval 300

This will call the distiller periodically to regenerate TASK_STATE.md and file_summaries.json.
"""
import argparse
import time
from pathlib import Path
from src.core.memory.distiller import distill_context


def run_once(workdir: Path):
    # Load recent messages if TASK_STATE.md exists as fallback, but distill_context expects messages list
    # We call it with empty messages to force file_summaries generation based on repo_index.json
    try:
        print(f"Refreshing summaries in {workdir}")
        distill_context([], working_dir=workdir)
    except Exception as e:
        print(f"Failed to refresh summaries: {e}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("workdir", type=str, help="Working directory where .agent-context lives")
    p.add_argument("--interval", type=int, default=300, help="Seconds between refresh runs")
    args = p.parse_args()
    wd = Path(args.workdir)
    if not wd.exists():
        print(f"Workdir not found: {wd}")
        return

    try:
        while True:
            run_once(wd)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped by user")


if __name__ == "__main__":
    main()

