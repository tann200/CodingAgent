"""
Web tools for the coding agent.

Provides web_search (DuckDuckGo) and read_web_page (URL fetching)
so the agent can look up documentation, error messages, and API docs.
"""

import logging
import re
import socket
import ipaddress
from typing import Any, Dict, List

from src.tools._tool import tool, PermissionKind

logger = logging.getLogger(__name__)

# Block private/internal IP ranges to prevent SSRF
_BLOCKED_HOSTS = re.compile(
    r"^(127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|0\.0\.0\.0|169\.254\.\d+\.\d+|localhost)$",
    re.IGNORECASE,
)

_MAX_WEB_PAGE_CHARS = 100_000


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
        if _BLOCKED_HOSTS.match(host):
            return True
        # Resolve all IP addresses for the hostname and check each one
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                addr_str = info[4][0]
                try:
                    addr = ipaddress.ip_address(addr_str)
                    if (
                        addr.is_loopback
                        or addr.is_private
                        or addr.is_link_local
                        or addr.is_reserved
                        or addr.is_unspecified
                        or addr.is_multicast
                    ):
                        return True
                except ValueError:
                    return True  # Unparseable address — block it
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
        if _BLOCKED_HOSTS.match(host):
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

                if (
                    addr.is_loopback
                    or addr.is_private
                    or addr.is_link_local
                    or addr.is_reserved
                    or addr.is_unspecified
                    or addr.is_multicast
                ):
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

    try:
        import requests

        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()

        content = resp.text
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

        truncated = len(content) > _MAX_WEB_PAGE_CHARS
        return {
            "status": "ok",
            "url": url,
            "content": content[:_MAX_WEB_PAGE_CHARS],
            "truncated": truncated,
        }
    except Exception as e:
        return {"status": "error", "error": f"read_web_page failed: {e}"}
