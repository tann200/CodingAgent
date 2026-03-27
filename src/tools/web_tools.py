"""
Web tools for the coding agent.

Provides web_search (DuckDuckGo) and read_web_page (URL fetching)
so the agent can look up documentation, error messages, and API docs.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from pathlib import Path

from src.tools._tool import tool

logger = logging.getLogger(__name__)

# Block private/internal IP ranges to prevent SSRF
_BLOCKED_HOSTS = re.compile(
    r"^(127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|0\.0\.0\.0|169\.254\.\d+\.\d+|localhost)$",
    re.IGNORECASE,
)

_MAX_WEB_PAGE_CHARS = 10_000


def _is_url_blocked(url: str) -> bool:
    """Check if URL points to a blocked scheme or private/internal address."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        # Block non-HTTP schemes: file://, ftp://, ssh://, etc.
        if parsed.scheme not in ("http", "https"):
            return True
        host = parsed.hostname or ""
        return bool(_BLOCKED_HOSTS.match(host))
    except Exception:
        return True  # Block on parse failure


@tool(tags=["coding", "planning", "debug"])
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
        from duckduckgo_search import DDGS

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


@tool(tags=["coding", "planning"])
def read_web_page(url: str) -> Dict[str, Any]:
    """Fetch and return the text content of a web page.

    Use after web_search to read full documentation or a specific page.
    Returns first 10,000 characters of extracted text.

    Args:
        url: The URL to fetch.

    Returns:
        status, url, content (truncated text), truncated (bool).
    """
    if not url or not url.strip():
        return {"status": "error", "error": "url must be non-empty"}

    if _is_url_blocked(url):
        return {
            "status": "error",
            "error": f"URL '{url}' points to a private/internal address. Blocked for security.",
        }

    try:
        import requests

        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()

        # Strip HTML tags for text extraction
        content = resp.text
        try:
            import html2text

            h = html2text.HTML2Text()
            h.ignore_links = True
            h.ignore_images = True
            content = h.handle(content)
        except ImportError:
            # Fallback: regex tag stripping
            content = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL)
            content = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL)
            content = re.sub(r"<[^>]+>", " ", content)
            content = re.sub(r"\s+", " ", content).strip()

        truncated = len(content) > _MAX_WEB_PAGE_CHARS
        return {
            "status": "ok",
            "url": url,
            "content": content[:_MAX_WEB_PAGE_CHARS],
            "truncated": truncated,
        }
    except Exception as e:
        return {"status": "error", "error": f"read_web_page failed: {e}"}
