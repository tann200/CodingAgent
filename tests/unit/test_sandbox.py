"""Unit tests for src/tools/sandbox.py — G7 macOS sandbox-exec fallback."""

import os
import platform
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.tools import sandbox as sbox


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _mock_success(*args, **kwargs):
    """Simulate a successful subprocess.run."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = ""
    m.stderr = ""
    return m


# ---------------------------------------------------------------------------
# sandbox-exec detection
# ---------------------------------------------------------------------------


class TestSandboxExecDetection:
    def test_sandbox_exec_available_on_macos(self):
        if platform.system() != "Darwin":
            pytest.skip("sandbox-exec only on macOS")
        assert sbox._sandbox_exec_available() is True

    def test_sandbox_exec_not_available_when_missing(self):
        import importlib
        with patch("shutil.which", return_value=None):
            importlib.reload(sbox)
            assert sbox._sandbox_exec_available() is False
            # Restore
            importlib.reload(sbox)


# ---------------------------------------------------------------------------
# Profile generation
# ---------------------------------------------------------------------------


class TestSandboxExcProfile:
    def test_profile_contains_cwd(self):
        profile = sbox._build_sandbox_exc_profile(Path("/tmp/testdir"), "workspace")
        assert "/tmp/testdir" in profile

    def test_profile_denies_network(self):
        profile = sbox._build_sandbox_exc_profile(Path("/tmp/wd"), "workspace")
        assert "deny network" in profile

    def test_profile_allows_read_only_dirs(self):
        profile = sbox._build_sandbox_exc_profile(Path("/tmp/wd"), "workspace")
        assert "usr" in profile.lower()

    def test_full_mode_adds_extra_restrictions(self):
        profile = sbox._build_sandbox_exc_profile(Path("/tmp/wd"), "full")
        assert "deny process-fork" in profile

    def test_profile_is_valid_sandbox_exec_syntax(self):
        profile = sbox._build_sandbox_exc_profile(Path("/tmp/wd"), "workspace")
        assert "(version 1)" in profile


# ---------------------------------------------------------------------------
# run_sandboxed routing
# ---------------------------------------------------------------------------


class TestRunSandboxedRouting:
    """Verify run_sandboxed chooses the right backend."""

    def test_off_level_uses_subprocess_directly(self):
        with patch("src.tools.sandbox.subprocess.run", side_effect=_mock_success) as mock_run:
            sbox.run_sandboxed(["echo", "hi"], cwd=Path("/tmp"), sandbox_level="off")
            mock_run.assert_called_once()

    def test_bwrap_used_when_available(self):
        with patch("src.tools.sandbox.subprocess.run", side_effect=_mock_success) as mock_run, \
             patch.object(sbox, "_bwrap_available", return_value=True), \
             patch.object(sbox, "_build_bwrap_args", return_value=["bwrap", "--version"]):
            sbox.run_sandboxed(["echo", "hi"], cwd=Path("/tmp"))
            mock_run.assert_called_once()
            cmd_used = mock_run.call_args[0][0]
            assert any("bwrap" in str(part).lower() for part in cmd_used)

    def test_sandbox_exc_used_when_bwrap_unavailable_on_macos(self):
        if platform.system() != "Darwin":
            pytest.skip("sandbox-exc only on macOS")
        fake_profile = "/tmp/fake.sb"
        # Just verify the function runs without error when sandbox-exc is available
        with patch.object(sbox, "_bwrap_available", return_value=False), \
             patch.object(sbox, "_sandbox_exec_available", return_value=True), \
             patch.object(sbox, "_write_sandbox_exc_profile", return_value=fake_profile), \
             patch("src.tools.sandbox.subprocess.run", side_effect=_mock_success):
            # Should not raise
            result = sbox.run_sandboxed(["echo", "hi"], cwd=Path("/tmp"))
            assert result.returncode == 0

    def test_fallback_to_subprocess_when_all_unavailable(self):
        with patch("src.tools.sandbox.subprocess.run", side_effect=_mock_success) as mock_run, \
             patch.object(sbox, "_bwrap_available", return_value=False), \
             patch.object(sbox, "_sandbox_exec_available", return_value=False):
            sbox.run_sandboxed(["echo", "hi"], cwd=Path("/tmp"))
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert not any("bwrap" in str(p).lower() for p in args)
            assert not any("sandbox-exc" in str(p).lower() for p in args)


# ---------------------------------------------------------------------------
# sandbox_available
# ---------------------------------------------------------------------------


class TestSandboxAvailable:
    def test_returns_true_when_bwrap_available(self):
        with patch.object(sbox, "_bwrap_available", return_value=True):
            assert sbox.sandbox_available() is True

    def test_returns_true_when_sandbox_exec_available(self):
        with patch.object(sbox, "_bwrap_available", return_value=False), \
             patch.object(sbox, "_sandbox_exec_available", return_value=True):
            assert sbox.sandbox_available() is True

    def test_returns_false_when_none_available(self):
        with patch.object(sbox, "_bwrap_available", return_value=False), \
             patch.object(sbox, "_sandbox_exec_available", return_value=False):
            assert sbox.sandbox_available() is False
