"""Small helper to exercise the file lock fallback path for diagnostics.

This script is intended to be invoked from tests to attempt acquiring the
per-workdir lock repeatedly and print diagnostics. It's not run by default in
CI; set the RUN_LOCK_DIAG=1 env var to execute the diagnostic test.
"""

import os
import sys
import time
from pathlib import Path

from src.tools.todo_tools import _lock_path, _FileLock


def run_diag(workdir: str, iterations: int = 10, pause: float = 0.01):
    lockp = _lock_path(workdir)
    for i in range(iterations):
        try:
            # Acquire the lock using the library helper
            with _FileLock(lockp, timeout=0.5):
                print(f"[{os.getpid()}] acquired lock iteration {i}")
                time.sleep(pause)
        except Exception as e:
            print(f"[{os.getpid()}] failed to acquire lock: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: lock_diag.py <workdir> [iterations]")
        sys.exit(2)
    workdir = sys.argv[1]
    iters = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    run_diag(workdir, iterations=iters)
