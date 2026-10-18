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


def test_is_ip_blocked_ipv6_transition():
    # IPv4-mapped loopback and metadata
    assert web_tools._is_ip_blocked("::ffff:169.254.169.254") is True
    assert web_tools._is_ip_blocked("::ffff:127.0.0.1") is True
    assert web_tools._is_ip_blocked("::ffff:10.0.0.1") is True
    # 6to4 mapped cloud metadata (2002:a9fe:a9fe::1 -> 169.254.169.254)
    assert web_tools._is_ip_blocked("2002:a9fe:a9fe::1") is True
    # Public IPv4 and IPv6
    assert web_tools._is_ip_blocked("93.184.216.34") is False
    assert web_tools._is_ip_blocked("2606:2800:220:1:248:1893:25c8:1946") is False


def test_read_web_page_blocks_dns_rebinding(monkeypatch):
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _resolver(public="93.184.216.34"))

    class _RebindingResp(_Resp):
        def __init__(self):
            super().__init__(200, text="SECRET_INTERNAL_DATA")

            class _FakeSock:
                def getpeername(self):
                    return ("127.0.0.1", 8080)

            class _FakeConn:
                sock = _FakeSock()

            class _FakeRaw:
                _connection = _FakeConn()

            self.raw = _FakeRaw()

    with patch("requests.get", return_value=_RebindingResp()):
        res = web_tools.read_web_page("https://example.com/rebinding")

    assert res["status"] == "error"
    assert "dns rebinding" in res["error"].lower() or "blocked" in res["error"].lower()


def test_read_web_page_blocks_https_to_http_downgrade(monkeypatch):
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _resolver())

    def _get(url, **kwargs):
        return _Resp(302, {"Location": "http://example.com/downgraded"})

    with patch("requests.get", _get):
        res = web_tools.read_web_page("https://example.com/")

    assert res["status"] == "error"
    assert "downgrade" in res["error"].lower()


def test_read_web_page_stream_bounding(monkeypatch):
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _resolver())

    class _StreamingResp(_Resp):
        def __init__(self):
            super().__init__(200)
            self.closed = False
            self.chunks_yielded = 0

        def iter_content(self, chunk_size=8192, decode_unicode=False):
            # Infinite or large generator yielding 10KB chunks
            for _ in range(100):  # 100 * 10KB = 1MB > 512KB cap
                self.chunks_yielded += 1
                yield b"A" * 10240

        def close(self):
            self.closed = True

    mock_resp = _StreamingResp()
    with patch("requests.get", return_value=mock_resp):
        res = web_tools.read_web_page("https://example.com/large")

    assert res["status"] == "ok"
    assert res["truncated"] is True
    assert mock_resp.closed is True
    # Should stop early at ~52 chunks (520KB), not all 100
    assert mock_resp.chunks_yielded < 100
