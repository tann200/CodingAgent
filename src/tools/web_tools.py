"""
Web tools for the coding agent.

Provides web_search (DuckDuckGo) and read_web_page (URL fetching)
so the agent can look up documentation, error messages, and API docs.
"""

import logging
import re
import socket
import ipaddress
from typing import Any, Dict, List, Tuple

from src.tools._tool import tool, PermissionKind

logger = logging.getLogger(__name__)

# Block private/internal IP ranges to prevent SSRF
_BLOCKED_HOSTS = re.compile(
    r"^(127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|0\.0\.0\.0|169\.254\.\d+\.\d+|localhost)$",
    re.IGNORECASE,
)

_MAX_WEB_PAGE_CHARS = 100_000
_MAX_WEB_PAGE_BYTES = 512 * 1024
_MAX_REDIRECTS = 5


def _is_ip_blocked(ip_str: str) -> bool:
    """Return True if ip_str is a loopback, private, link-local, reserved,
    cloud metadata, or IPv6-transition address pointing to internal networks.
    """
    if not ip_str or not isinstance(ip_str, str):
        return False
    clean = ip_str.strip().strip("[]")
    try:
        addr = ipaddress.ip_address(clean)
    except ValueError:
        try:
            if clean.isdigit():
                addr = ipaddress.ip_address(int(clean))
            else:
                return False
        except (ValueError, OverflowError):
            return False

    # Unwrap IPv6 transition addresses
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return _is_ip_blocked(str(addr.ipv4_mapped))
        if addr.sixtofour is not None:
            return _is_ip_blocked(str(addr.sixtofour))
        if addr.teredo is not None:
            server_ip, client_ip = addr.teredo
            return _is_ip_blocked(str(server_ip)) or _is_ip_blocked(str(client_ip))

    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
        or str(addr) == "169.254.169.254"
    ):
        return True

    return False


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace to plain text."""
    content = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    content = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL)
    content = re.sub(r"<[^>]+>", " ", content)
    return re.sub(r"\s+", " ", content).strip()


def _is_url_blocked(url: str) -> bool:
    """Check if URL points to a blocked scheme or private/internal address.

    Checks both the raw hostname string and all resolved IP addresses so that
    decimal-encoded IPs (e.g. http://2130706433/), IPv6 loopback (::1), and
    DNS rebinding attacks are all blocked.
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        # Block non-HTTP schemes: file://, ftp://, ssh://, etc.
        if parsed.scheme not in ("http", "https"):
            return True
        host = parsed.hostname or ""
        # Check raw hostname pattern first (fast path)
        if _BLOCKED_HOSTS.match(host) or _is_ip_blocked(host):
            return True
        # Resolve all IP addresses for the hostname and check each one
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                addr_str = info[4][0]
                if _is_ip_blocked(addr_str):
                    return True
        except (socket.gaierror, OSError):
            # DNS resolution failed — allow the request to proceed and let
            # the HTTP client handle the connection error naturally
            pass
        return False
    except Exception:
        return True  # Block on any parse failure


def _is_ssrf_blocked(url: str) -> None:
    """Raise PermissionError when *url* points to a blocked/internal address.

    This is the preferred SSRF protection entrypoint. It consults the optional
    configuration key ``web.ssrf_allowlist`` (list) first; any matching
    hostname, domain suffix, IP, or CIDR in the allowlist permits the URL.

    On parse or resolution failures we err on the side of permissive (allow the
    request) so that transient DNS issues do not block normal operation.  When a
    blocked address is detected a PermissionError is raised.
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PermissionError(f"Blocked URL scheme: {parsed.scheme}")

        host = parsed.hostname or ""

        # Load optional allowlist from config (web.ssrf_allowlist)
        try:
            from src.core.config_loader import load_merged_config

            cfg = load_merged_config() or {}
            allowlist = (cfg.get("web") or {}).get("ssrf_allowlist") or []
        except Exception:
            allowlist = []

        def _host_allowed_by_allowlist(h: str) -> bool:
            if not allowlist:
                return False
            for ent in allowlist:
                if not isinstance(ent, str):
                    continue
                ent = ent.strip()
                if not ent:
                    continue
                # Exact hostname match
                if h == ent:
                    return True
                # Domain suffix match (allow example.com and sub.example.com)
                if h.endswith("." + ent) or h == ent:
                    return True
            return False

        if _host_allowed_by_allowlist(host):
            return

        # Fast-path textual checks
        if _BLOCKED_HOSTS.match(host) or _is_ip_blocked(host):
            raise PermissionError(f"URL '{url}' points to a blocked host: {host}")

        # Resolve addresses and check each one. Only catch socket.gaierror
        # around the getaddrinfo call so PermissionError raised during
        # address inspection is not swallowed (PermissionError subclasses OSError).
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            # DNS resolution failed — allow and let the HTTP client report errors
            infos = []

        for info in infos:
            addr_str = info[4][0]
            try:
                addr = ipaddress.ip_address(addr_str)

                # If allowlist contains CIDR or IP entries, respect them
                allowed_by_net = False
                if allowlist:
                    for ent in allowlist:
                        try:
                            # IP or network
                            if "/" in ent:
                                net = ipaddress.ip_network(ent, strict=False)
                                if addr in net:
                                    allowed_by_net = True
                                    break
                            else:
                                # exact IP match
                                ipent = ipaddress.ip_address(ent)
                                if addr == ipent:
                                    allowed_by_net = True
                                    break
                        except Exception:
                            continue
                if allowed_by_net:
                    continue

                if _is_ip_blocked(addr_str):
                    raise PermissionError(
                        f"URL '{url}' resolved to internal address {addr_str}"
                    )
            except ValueError:
                # Unparseable address — block conservatively
                raise PermissionError(
                    f"URL '{url}' resolved to unparseable address: {addr_str}"
                )
        return
    except PermissionError:
        raise
    except Exception:
        # On unexpected errors, block conservatively
        raise PermissionError(f"URL '{url}' blocked due to parse/resolve error")


@tool(tags=["coding", "planning", "debug"], permission_kind=PermissionKind.NETWORK)
def web_search(query: str, max_results: int = 5) -> Dict[str, Any]:
    """Search the web for documentation, error messages, or package information.

    Use for: looking up API docs, searching for error message solutions,
    checking PyPI/npm package availability, finding GitHub issues.
    Returns titles, URLs, and short snippets.

    Args:
        query: Search query string.
        max_results: Maximum number of results (default 5, max 10).

    Returns:
        status, results (list of {title, url, snippet}).
    """
    if not query or not query.strip():
        return {"status": "error", "error": "query must be non-empty"}

    max_results = min(max(max_results, 1), 10)

    # Try duckduckgo-search package first
    try:
        from duckduckgo_search import DDGS  # type: ignore[import]

        results: List[Dict[str, str]] = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", r.get("link", "")),
                        "snippet": r.get("body", "")[:300],
                    }
                )
        return {"status": "ok", "query": query, "results": results}
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"web_search: duckduckgo-search failed: {e}")

    # Fallback: direct DuckDuckGo HTML scraping
    try:
        import requests

        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()
        # Very simple extraction — not production-grade but works as fallback
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>',
            resp.text,
            re.DOTALL,
        )
        urls = re.findall(
            r'class="result__url"[^>]*>(.*?)</a>',
            resp.text,
            re.DOTALL,
        )
        results = []
        for i in range(min(max_results, len(snippets))):
            results.append(
                {
                    "title": "",
                    "url": re.sub(r"<[^>]+>", "", urls[i]).strip()
                    if i < len(urls)
                    else "",
                    "snippet": re.sub(r"<[^>]+>", "", snippets[i]).strip()[:300],
                }
            )
        return {"status": "ok", "query": query, "results": results}
    except Exception as e:
        return {"status": "error", "error": f"web_search failed: {e}"}


def _fetch_with_validated_redirects(url: str, timeout: int = 15) -> Tuple[Any, str]:
    """GET *url*, following redirects manually and validating every hop.

    ``requests`` follows redirects by default, so a public URL could redirect
    to a loopback/metadata/private address and bypass the SSRF check that only
    inspects the initial URL.  Automatic redirects are disabled here and each
    ``Location`` target is validated (scheme plus all resolved peer addresses,
    via :func:`_is_ssrf_blocked`) before it is followed.

    Returns ``(response, final_url)``.  Raises ``PermissionError`` when a hop is
    blocked or the redirect chain exceeds ``_MAX_REDIRECTS``.
    """
    import requests
    from urllib.parse import urljoin, urlparse

    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        # Validate this hop before connecting (hostname + resolved peer IPs).
        _is_ssrf_blocked(current)
        resp = requests.get(
            current,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )

        # Inspect connected socket peer address immediately to prevent DNS rebinding TOCTOU
        raw = getattr(resp, "raw", None)
        underlying_conn = getattr(raw, "_connection", None) or getattr(raw, "connection", None)
        sock = getattr(underlying_conn, "sock", None) if underlying_conn else None
        if sock and hasattr(sock, "getpeername"):
            peer = None
            try:
                peer = sock.getpeername()
            except (socket.error, OSError):
                pass
            if peer and isinstance(peer, (tuple, list)) and len(peer) > 0:
                peer_ip = str(peer[0])
                if _is_ip_blocked(peer_ip):
                    if hasattr(resp, "close"):
                        resp.close()
                    raise PermissionError(
                        f"Connected peer IP '{peer_ip}' for URL '{current}' is blocked (SSRF DNS rebinding protection)"
                    )

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                return resp, current
            next_url = urljoin(current, location)
            # Block HTTPS -> HTTP downgrade across redirect hops
            parsed_current = urlparse(current)
            parsed_next = urlparse(next_url)
            if parsed_current.scheme == "https" and parsed_next.scheme == "http":
                if hasattr(resp, "close"):
                    resp.close()
                raise PermissionError(
                    f"Insecure redirect downgrade from HTTPS to HTTP blocked: {current} -> {next_url}"
                )
            if hasattr(resp, "close"):
                resp.close()
            current = next_url
            continue
        return resp, current
    raise PermissionError(
        f"Too many redirects (>{_MAX_REDIRECTS}) for URL '{url}'"
    )


@tool(tags=["coding", "planning"], permission_kind=PermissionKind.NETWORK)
def read_web_page(url: str, format: str = "markdown") -> Dict[str, Any]:
    """Fetch and return the text content of a web page.

    Use after web_search to read full documentation or a specific page.
    Returns up to 100,000 characters of extracted text.

    Args:
        url: The URL to fetch. HTTP URLs are upgraded to HTTPS automatically.
        format: Output format — "markdown" (default, uses html2text) or "text" (plain text).

    Returns:
        status, url, content (extracted text), truncated (bool).
    """
    if not url or not url.strip():
        return {"status": "error", "error": "url must be non-empty"}

    # Upgrade HTTP to HTTPS
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]

    try:
        _is_ssrf_blocked(url)
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}

    if format not in ("markdown", "text"):
        return {"status": "error", "error": "format must be 'markdown' or 'text'"}

    resp = None
    try:
        resp, final_url = _fetch_with_validated_redirects(url)
        resp.raise_for_status()

        # Read response body in chunks up to _MAX_WEB_PAGE_BYTES
        chunks: List[str] = []
        total_bytes = 0
        stream_capped = False
        if hasattr(resp, "iter_content"):
            for raw_chunk in resp.iter_content(chunk_size=8192, decode_unicode=False):
                if not raw_chunk:
                    continue
                if isinstance(raw_chunk, bytes):
                    total_bytes += len(raw_chunk)
                    encoding = getattr(resp, "encoding", None) or "utf-8"
                    chunk_str = raw_chunk.decode(encoding, errors="replace")
                else:
                    chunk_str = str(raw_chunk)
                    total_bytes += len(chunk_str.encode("utf-8", errors="replace"))
                chunks.append(chunk_str)
                if total_bytes >= _MAX_WEB_PAGE_BYTES:
                    stream_capped = True
                    break
            content = "".join(chunks)
        else:
            content = getattr(resp, "text", "")

        if format == "markdown":
            try:
                import html2text  # type: ignore[import]

                h = html2text.HTML2Text()
                h.ignore_links = False
                h.ignore_images = True
                content = h.handle(content)
            except ImportError:
                content = _strip_html(content)
        else:
            content = _strip_html(content)

        truncated = len(content) > _MAX_WEB_PAGE_CHARS or stream_capped
        return {
            "status": "ok",
            "url": final_url,
            "content": content[:_MAX_WEB_PAGE_CHARS],
            "truncated": truncated,
        }
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}
    except Exception as e:
        return {"status": "error", "error": f"read_web_page failed: {e}"}
    finally:
        if resp is not None and hasattr(resp, "close"):
            try:
                resp.close()
            except Exception:
                pass
