"""RS-1: Remote skill discovery and caching.

Fetches skill indexes and individual skill files from URLs configured under
``skills.urls`` in the agent config.  Skills are cached locally so repeated
runs don't hit the network.

Index format
------------
Each base URL must expose ``<url>/index.json`` with content like::

    [
        {"name": "my_skill",    "file": "my_skill.md",    "description": "..."},
        {"name": "other_skill", "file": "other_skill.md"}
    ]

The ``file`` field is fetched from ``<base_url>/<file>`` and written to the
cache dir.  ``name`` becomes the skill key in ``AgentBrainManager``.

Cache layout
------------
Cached skills are stored under the CodingAgent cache directory (get_skills_cache_dir()),
in the layout: <skills_cache_dir>/<hash-of-base-url>/<name>.md

Where <hash-of-base-url> is the first 8 chars of the MD5 of the base URL so
skills from different sources don't collide.

Usage
-----
This module is called from ``AgentBrainManager._load_all()`` after local
skills are loaded.  Remote skills are *additive* — they never shadow local
skills (local skills take precedence by being loaded first).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from src.core.paths import get_skills_cache_dir
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

_CACHE_BASE = get_skills_cache_dir()
_DEFAULT_TTL = 3600  # seconds
_FETCH_TIMEOUT = 10  # seconds per HTTP request


def _cache_dir_for_url(base_url: str) -> Path:
    """Return a stable per-URL cache sub-directory."""
    url_hash = hashlib.md5(base_url.encode()).hexdigest()[:8]
    return _CACHE_BASE / url_hash


def _is_stale(path: Path, ttl: int) -> bool:
    """Return True if *path* doesn't exist or is older than *ttl* seconds."""
    if not path.exists():
        return True
    age = time.time() - path.stat().st_mtime
    return age > ttl


def _fetch_text(url: str) -> Optional[str]:
    """Fetch *url* and return the response text, or None on error."""
    try:
        # SSRF protection: block URLs that resolve to internal/private addresses.
        try:
            from src.tools.web_tools import _is_ssrf_blocked

            _is_ssrf_blocked(url)
        except PermissionError as pe:
            logger.warning("remote_skills: SSRF blocked for %s: %s", url, pe)
            return None
        except Exception:
            # If SSRF checker is unavailable for any reason, proceed and let
            # requests report network errors. Do not fail hard here.
            pass
        import requests

        r = requests.get(url, timeout=_FETCH_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as exc:
        logger.debug("remote_skills: fetch failed for %s: %s", url, exc)
        return None


def fetch_remote_skills(
    base_url: str,
    ttl_seconds: int = _DEFAULT_TTL,
) -> Dict[str, str]:
    """Fetch all skills from *base_url* and cache them locally.

    Parameters
    ----------
    base_url:
        Base URL of the skill index (e.g.
        ``https://raw.githubusercontent.com/myorg/skills/main/``).
        Must expose ``<base_url>/index.json``.
    ttl_seconds:
        Cache TTL.  Skills already cached and younger than this value are
        not re-fetched from the network.

    Returns
    -------
    dict
        Mapping of skill_name → skill_markdown_content for all skills that
        were successfully loaded (from cache or fetched fresh).
    """
    if not base_url:
        return {}

    base_url = base_url.rstrip("/") + "/"
    cache_dir = _cache_dir_for_url(base_url)
    index_cache = cache_dir / "_index.json"

    # -- 1. Load / refresh index --
    index: List[Dict[str, Any]] = []
    if _is_stale(index_cache, ttl_seconds):
        index_url = urljoin(base_url, "index.json")
        raw = _fetch_text(index_url)
        if raw is None:
            logger.warning("remote_skills: could not fetch index from %s", index_url)
            # Try loading stale cache as fallback
            if index_cache.exists():
                try:
                    index = json.loads(index_cache.read_text(encoding="utf-8"))
                    logger.info("remote_skills: using stale index for %s", base_url)
                except Exception:
                    return {}
            else:
                return {}
        else:
            try:
                index = json.loads(raw)
                if not isinstance(index, list):
                    logger.warning(
                        "remote_skills: index.json from %s is not a list", base_url
                    )
                    return {}
                cache_dir.mkdir(parents=True, exist_ok=True)
                index_cache.write_text(raw, encoding="utf-8")
            except Exception as exc:
                logger.warning(
                    "remote_skills: bad index JSON from %s: %s", base_url, exc
                )
                return {}
    else:
        try:
            index = json.loads(index_cache.read_text(encoding="utf-8"))
        except Exception:
            return {}

    # -- 2. For each skill entry, load from cache or fetch --
    result: Dict[str, str] = {}
    for entry in index:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        file_ref = entry.get("file")
        if not name or not file_ref:
            continue

        skill_cache = cache_dir / f"{name}.md"
        if _is_stale(skill_cache, ttl_seconds):
            skill_url = urljoin(base_url, file_ref)
            content = _fetch_text(skill_url)
            if content is None:
                # Try stale cache as fallback
                if skill_cache.exists():
                    content = skill_cache.read_text(encoding="utf-8")
                    logger.debug("remote_skills: using stale cache for %s", name)
                else:
                    logger.warning("remote_skills: could not fetch skill %s", name)
                    continue
            else:
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    skill_cache.write_text(content, encoding="utf-8")
                except Exception as exc:
                    logger.debug(
                        "remote_skills: cache write failed for %s: %s", name, exc
                    )
        else:
            try:
                content = skill_cache.read_text(encoding="utf-8")
            except Exception:
                continue

        result[name] = content
        logger.info("remote_skills: loaded %s from %s", name, base_url)

    return result


def load_all_remote_skills(
    urls: Optional[List[str]] = None,
    ttl_seconds: int = _DEFAULT_TTL,
) -> Dict[str, str]:
    """Load remote skills from all configured URLs.

    Parameters
    ----------
    urls:
        List of base URLs.  If None, reads from the agent config
        (``skills.urls``).
    ttl_seconds:
        Cache TTL.  Overrides config ``skills.cache_ttl_seconds`` when set.

    Returns
    -------
    dict
        Merged mapping of skill_name → content from all URLs.
        Later URLs override earlier ones on name collision.
    """
    if urls is None:
        try:
            from src.core.config_loader import load_merged_config

            cfg = load_merged_config()
            skills_cfg = cfg.get("skills", {})
            urls = skills_cfg.get("urls", []) or []
            ttl_seconds = int(skills_cfg.get("cache_ttl_seconds", ttl_seconds))
        except Exception:
            urls = []

    if not urls:
        return {}

    merged: Dict[str, str] = {}
    for url in urls:
        try:
            skills = fetch_remote_skills(url, ttl_seconds=ttl_seconds)
            merged.update(skills)
        except Exception as exc:
            logger.warning("remote_skills: failed to load skills from %s: %s", url, exc)

    return merged
