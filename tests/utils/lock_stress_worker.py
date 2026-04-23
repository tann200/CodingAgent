#!/usr/bin/env python3
"""Worker process for lock stress testing.

This script performs many acquire/release cycles using the fallback lock
implementation (forces _fcntl=None) and prints per-process metrics as JSON.
"""

import json
import os
import random
import sys
import time
from pathlib import Path


def main():
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))

    from src.tools.todo_tools import (
        _lock_path,
        _FileLock,
        reset_lock_metrics,
        get_lock_metrics,
    )

    if len(sys.argv) < 3:
        print("Usage: lock_stress_worker.py <workdir> <iterations>", file=sys.stderr)
        sys.exit(2)

    workdir = sys.argv[1]
    iterations = int(sys.argv[2])

    reset_lock_metrics()
    lockp = _lock_path(workdir)
    lockp.parent.mkdir(parents=True, exist_ok=True)

    for i in range(iterations):
        lock = _FileLock(lockp, timeout=5.0)
        # Force fallback path for stress testing
        lock._fcntl = None
        try:
            with lock:
                # small randomized hold to increase contention
                time.sleep(random.random() * 0.002)
        except Exception as e:
            # Don't fail; print to stderr for debugging
            print(f"ERR: {e}", file=sys.stderr)

    metrics = get_lock_metrics()
    print(json.dumps({"pid": os.getpid(), "metrics": metrics}))


if __name__ == "__main__":
    main()
