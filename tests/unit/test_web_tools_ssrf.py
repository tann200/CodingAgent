import socket
import ipaddress
from unittest.mock import patch

import pytest

from src.tools import web_tools


def _fake_getaddrinfo_return(addr_list):
    # return structure similar to socket.getaddrinfo: list of tuples where
    # index 4 is (addr, port) or (addr,)
    out = []
    for a in addr_list:
        # family, socktype, proto, canonname, sockaddr
        out.append((0, 0, 0, "", (str(a), 0)))
    return out


def test_is_ssrf_blocked_ipv4_loopback(monkeypatch):
    url = "http://127.0.0.1/"
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: _fake_getaddrinfo_return(["127.0.0.1"]),
    )
    with pytest.raises(PermissionError):
        web_tools._is_ssrf_blocked(url)


def test_is_ssrf_blocked_ipv6_loopback(monkeypatch):
    url = "http://[::1]/"
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: _fake_getaddrinfo_return(["::1"]),
    )
    with pytest.raises(PermissionError):
        web_tools._is_ssrf_blocked(url)


def test_is_ssrf_blocked_decimal_ip(monkeypatch):
    # 2130706433 == 127.0.0.1 in decimal form
    url = "http://2130706433/"
    # Some resolvers may return the decimal directly; treat as blocked
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: _fake_getaddrinfo_return(["127.0.0.1"]),
    )
    with pytest.raises(PermissionError):
        web_tools._is_ssrf_blocked(url)


def test_is_ssrf_allows_external(monkeypatch):
    url = "https://example.com/"
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: _fake_getaddrinfo_return(["93.184.216.34"]),
    )
    # Should not raise
    web_tools._is_ssrf_blocked(url)


def test_is_ssrf_respects_allowlist(monkeypatch):
    url = "https://internal.example.local/"
    # Resolve to a private IP, but allowlist contains the domain
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: _fake_getaddrinfo_return(["10.0.0.5"]),
    )
    with patch(
        "src.core.config_loader.load_merged_config",
        return_value={
            "web": {"ssrf_allowlist": ["internal.example.local", "10.0.0.0/8"]}
        },
    ):
        # Should not raise due to allowlist
        web_tools._is_ssrf_blocked(url)
