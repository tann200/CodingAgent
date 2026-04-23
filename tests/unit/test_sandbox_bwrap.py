import subprocess
from pathlib import Path

import pytest

from src.tools import sandbox


def test_build_bwrap_args_basic(tmp_path, monkeypatch):
    # Ensure predictable bwrap binary path for the test
    monkeypatch.setattr(sandbox, "_BWRAP_PATH", "/usr/bin/bwrap", raising=False)

    cwd = tmp_path
    extra = str(tmp_path / "extra")
    args = sandbox._build_bwrap_args(cwd, "workspace", extra_writable_dirs=[extra])

    # first element should be the bwrap path we set
    assert args[0] == "/usr/bin/bwrap"
    # ensure die-with-parent present
    assert "--die-with-parent" in args
    # working directory should be mounted and chdir present
    cwd_str = str(cwd.resolve())
    assert "--bind" in args or "--ro-bind" in args
    assert cwd_str in args
    assert "--chdir" in args
    assert args[args.index("--chdir") + 1] == cwd_str


def test_run_sandboxed_network_flag(monkeypatch, tmp_path):
    # Simulate that bwrap is available
    monkeypatch.setattr(sandbox, "_BWRAP_AVAILABLE", True, raising=False)
    monkeypatch.setattr(sandbox, "_BWRAP_PATH", "/usr/bin/bwrap", raising=False)

    captured = {}

    def fake_run(cmd, cwd=None, timeout=None, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    # Patch subprocess.run used inside the sandbox module
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    # network disabled: expect --unshare-net in the bwrap prefix
    sandbox.run_sandboxed(["/bin/true"], tmp_path, network=False)
    assert any(x == "--unshare-net" for x in captured["cmd"])

    # network enabled: expect --unshare-net removed from prefix
    sandbox.run_sandboxed(["/bin/true"], tmp_path, network=True)
    assert not any(x == "--unshare-net" for x in captured["cmd"])


def test_run_sandboxed_fallback_when_bwrap_missing(monkeypatch, tmp_path):
    # Simulate bwrap not available
    monkeypatch.setattr(sandbox, "_BWRAP_AVAILABLE", False, raising=False)

    captured = {}

    def fake_run(cmd, cwd=None, timeout=None, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    # When bwrap isn't available, run_sandboxed should call subprocess.run with the
    # original command (not prefixed with bwrap args)
    sandbox.run_sandboxed(["echo", "hi"], tmp_path)
    assert captured["cmd"] == ["echo", "hi"]
