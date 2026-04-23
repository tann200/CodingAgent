import subprocess
from pathlib import Path

# ruff: noqa: E501
import os

import pytest
import sys


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


def test_bwrap_flags_valid(monkeypatch, tmp_path):
    """Ensure bwrap args are separate tokens (no combined '--proc /proc' style tokens)."""
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
    sandbox._BWRAP_AVAILABLE = sandbox._probe_bwrap(sandbox._BWRAP_PATH)
    assert sandbox.sandbox_available()

    args = sandbox._build_bwrap_args(tmp_path, "workspace")

    # No argument should contain a space (which indicates a combined flag+arg token)
    assert all(" " not in str(a) for a in args)


def test_probe_bwrap_missing(monkeypatch):
    """_probe_bwrap should return False when bwrap --version returns non-zero."""
    import importlib

    import src.tools.sandbox as sandbox

    importlib.reload(sandbox)

    monkeypatch.setattr(sandbox, "_BWRAP_PATH", "/usr/bin/bwrap")

    def bad_probe(args, stdout=None, stderr=None, timeout=None):
        class R:
            returncode = 1

        return R()

    monkeypatch.setattr(subprocess, "run", bad_probe)
    assert sandbox._probe_bwrap(sandbox._BWRAP_PATH) is False


def test_bwrap_exec_missing_falls_back(monkeypatch, tmp_path):
    """If bwrap disappears between check and exec, run_sandboxed should fall back to subprocess.run."""
    import importlib

    import src.tools.sandbox as sandbox

    importlib.reload(sandbox)

    # Force the module to think bwrap is available
    monkeypatch.setattr(sandbox, "_BWRAP_AVAILABLE", True)

    calls = []

    def run_side_effect(cmd, cwd=None, timeout=None, **kwargs):
        # Record the raw command passed
        calls.append(cmd)
        # First call simulates bwrap exec failing (FileNotFoundError)
        if len(calls) == 1:
            raise FileNotFoundError()
        # Fallback call returns a CompletedProcess
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", run_side_effect)

    res = sandbox.run_sandboxed(["echo", "fallback"], cwd=tmp_path)

    # We should have attempted the bwrap invocation first (a list starting with the bwrap binary)
    assert len(calls) >= 2
    assert isinstance(calls[0], list)
    # The fallback should be the original command
    assert calls[1] == ["echo", "fallback"]


def test_startup_warning_published(monkeypatch, tmp_path):
    """When bwrap is missing but sandboxing is requested, a startup warning should be published."""
    import importlib

    # Ensure bwrap is not found on PATH
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("CODINGAGENT_SANDBOX_LEVEL", "workspace")

    published = []

    class FakeBus:
        def publish(self, event_name, payload, correlation_id=None):
            published.append((event_name, payload))

    # Monkeypatch the event_bus.get_event_bus before importing sandbox
    import types

    fake_event_bus_module = types.SimpleNamespace()
    fake_event_bus_module.get_event_bus = lambda: FakeBus()

    monkeypatch.setitem(
        sys.modules, "src.core.orchestration.event_bus", fake_event_bus_module
    )

    import src.tools.sandbox as sandbox

    importlib.reload(sandbox)

    # When bwrap is missing and level != 'off', a system.warning should be published
    assert any(ev == "system.warning" for ev, _ in published)
