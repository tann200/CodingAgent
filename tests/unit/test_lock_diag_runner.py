import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("RUN_LOCK_DIAG") != "1", reason="lock diag disabled")
def test_lock_diag_runner(tmp_path):
    """Optional diagnostic: spawn a few processes running the lock diag helper.

    This test is disabled by default; enable by setting RUN_LOCK_DIAG=1 in
    the environment when running pytest locally.
    """
    workdir = tmp_path / "repo"
    workdir.mkdir()
    script = Path("tests/utils/lock_diag.py").resolve()
    procs = []
    for i in range(4):
        cmd = [sys.executable, str(script), str(workdir), "10"]
        env = dict(**{k: v for k, v in dict(**__import__("os").environ).items()})
        env["PYTHONPATH"] = str(Path.cwd())
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        procs.append(p)

    for p in procs:
        out, err = p.communicate(timeout=10)
        assert p.returncode == 0, (
            f"diag process failed: {err.decode() if err else out.decode()}"
        )
