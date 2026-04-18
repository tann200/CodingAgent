import subprocess
from pathlib import Path
import os

import pytest


def test_probe_and_fallback(monkeypatch, tmp_path):
    # Simulate bwrap not present
    monkeypatch.setenv("CODINGAGENT_SANDBOX_LEVEL", "workspace")
    monkeypatch.setenv("PATH", "")

    # Reload the module to pick up the mocked PATH
    import importlib

    import src.tools.sandbox as sandbox

    importlib.reload(sandbox)

    # Ensure sandbox is not available
    assert not sandbox.sandbox_available()

    # When not available, run_sandboxed should call subprocess.run with the original cmd
    called = {}

    def fake_run(cmd, cwd=None, timeout=None, **kwargs):
        called["cmd"] = cmd
        called["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = sandbox.run_sandboxed(["echo", "hello"], cwd=tmp_path)
    assert called["cmd"] == ["echo", "hello"]


def test_build_bwrap_args_and_execution(monkeypatch, tmp_path):
    # Simulate bwrap present and functional
    monkeypatch.setenv("CODINGAGENT_SANDBOX_LEVEL", "workspace")
    # Point _BWRAP_PATH to a fake path and probe to True by patching subprocess.run used in probe
    import importlib
    import src.tools.sandbox as sandbox

    importlib.reload(sandbox)

    monkeypatch.setattr(sandbox, "_BWRAP_PATH", "/usr/bin/bwrap")

    # Make the probe succeed
    def probe_run(args, stdout=None, stderr=None, timeout=None):
        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(subprocess, "run", probe_run)
    # Force re-evaluation of availability
    sandbox._BWRAP_AVAILABLE = sandbox._probe_bwrap(sandbox._BWRAP_PATH)
    assert sandbox.sandbox_available()

    executed = {}

    def fake_run(full_cmd, cwd=None, timeout=None, **kwargs):
        executed["full_cmd"] = full_cmd
        executed["cwd"] = cwd
        return subprocess.CompletedProcess(full_cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = sandbox.run_sandboxed(["echo", "ok"], cwd=tmp_path)
    # Ensure the bwrap prefix exists and we used '--' separator
    assert isinstance(executed.get("full_cmd"), list)
    assert "--" in executed["full_cmd"]
    # The final elements should include the '--' separator followed by the original command
    assert executed["full_cmd"][-3:] == ["--", "echo", "ok"]
    # Check die-with-parent and chdir present
    assert "--die-with-parent" in executed["full_cmd"]
    assert "--chdir" in executed["full_cmd"]
