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


class _Resp:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _resolver(public="93.184.216.34"):
    """getaddrinfo fake: loopback for local names, public otherwise."""

    def _getaddrinfo(host, *args, **kwargs):
        if host in ("127.0.0.1", "localhost", "::1"):
            return _fake_getaddrinfo_return(["127.0.0.1"])
        return _fake_getaddrinfo_return([public])

    return _getaddrinfo


def test_read_web_page_blocks_redirect_to_loopback(monkeypatch):
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _resolver())
    calls = []

    def _get(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp(302, {"Location": "http://127.0.0.1/secret"})

    with patch("requests.get", _get):
        res = web_tools.read_web_page("https://example.com/")

    assert res["status"] == "error"
    assert "127.0.0.1" in res["error"]
    # redirects must not be followed automatically by requests
    assert calls and calls[0][1].get("allow_redirects") is False


def test_read_web_page_follows_validated_redirect(monkeypatch):
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _resolver())
    responses = [
        _Resp(302, {"Location": "https://example.com/final"}),
        _Resp(200, text="<html><body>Hello</body></html>"),
    ]
    calls = []

    def _get(url, **kwargs):
        calls.append((url, kwargs))
        return responses[len(calls) - 1]

    with patch("requests.get", _get):
        res = web_tools.read_web_page("https://example.com/start")

    assert res["status"] == "ok"
    assert res["url"] == "https://example.com/final"
    assert "Hello" in res["content"]
    assert all(c[1].get("allow_redirects") is False for c in calls)


def test_read_web_page_too_many_redirects(monkeypatch):
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _resolver())

    def _get(url, **kwargs):
        return _Resp(302, {"Location": "https://example.com/loop"})

    with patch("requests.get", _get):
        res = web_tools.read_web_page("https://example.com/")

    assert res["status"] == "error"
    assert "redirect" in res["error"].lower()


def test_read_web_page_blocks_redirect_scheme_change(monkeypatch):
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _resolver())

    def _get(url, **kwargs):
        return _Resp(302, {"Location": "file:///etc/passwd"})

    with patch("requests.get", _get):
        res = web_tools.read_web_page("https://example.com/")

    assert res["status"] == "error"
    assert "scheme" in res["error"].lower()
