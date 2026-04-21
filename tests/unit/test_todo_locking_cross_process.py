import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.tools.todo_tools import _todo_json_path, _todo_path


def _write_worker_script(path: Path):
    path.write_text(
        """
import sys, time, random

# Ensure repo root is on sys.path so 'src' imports work in subprocesses
from pathlib import Path
repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root))

from src.tools.todo_tools import manage_todo

workdir = sys.argv[1]
pid = sys.argv[2]
iters = int(sys.argv[3])
for i in range(iters):
    try:
        manage_todo('create', workdir, steps=[f'proc{pid}_i{i}'], depends_on=[[]])
    except Exception as e:
        # print to stderr for debugging
        print('ERR', e, file=sys.stderr)
    time.sleep(random.random() * 0.01)
"""
    )


def test_cross_process_todo_writes(tmp_path):
    """Spawn multiple processes that concurrently write TODOs and assert
    the resulting todo.json is well-formed and no temp files remain.
    """
    workdir = tmp_path / "repo"
    workdir.mkdir()
    script = tmp_path / "worker.py"
    _write_worker_script(script)

    # Spawn multiple processes that will each perform several creates
    procs = []
    num_procs = 4
    iterations = 25
    for pidx in range(num_procs):
        cmd = [sys.executable, str(script), str(workdir), str(pidx), str(iterations)]
        # Ensure subprocesses can import the local 'src' package
        env = dict(**{k: v for k, v in dict(**__import__("os").environ).items()})
        env["PYTHONPATH"] = str(Path.cwd())
        procs.append(
            subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
            )
        )

    # Wait for processes to finish
    for p in procs:
        p.wait()
        # Ensure no unexpected stderr output
        out, err = p.communicate()
        assert p.returncode == 0, (
            f"Worker failed: {err.decode('utf-8') if err else out.decode('utf-8')}"
        )

    # Validate todo.json is valid JSON and has expected structure
    jp = _todo_json_path(str(workdir))
    assert jp.exists(), "todo.json was not created"
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) >= 1
    assert all(isinstance(item, dict) and "description" in item for item in data)

    # No leftover temp files
    agent_ctx = workdir / ".agent-context"
    assert agent_ctx.exists()
    tmp_files = [p for p in agent_ctx.iterdir() if ".tmp." in p.name]
    assert not tmp_files, f"Found leftover temp files: {tmp_files}"
