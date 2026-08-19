"""env_shims.py — Backward-compatible env var helpers.

Standardizes on the ``CODINGAGENT_`` prefix.  Any site that used the old
``CODING_AGENT_`` prefix is supported via ``getenv_with_compat``, which
reads the new name first, falls back to the old name, and emits a one-time
deprecation warning when the old name is consumed.

Usage::

    from src.core.env_shims import getenv_with_compat

    token = getenv_with_compat("CODINGAGENT_ADMIN_TOKEN", "CODING_AGENT_ADMIN_TOKEN")
"""

from __future__ import annotations

import logging
import os
from typing import Optional

_logger = logging.getLogger(__name__)

# Track which old names we've warned about (one-shot per process).
_warned: set[str] = set()


def getenv_with_compat(
    new_name: str,
    old_name: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Read *new_name* from the environment; fall back to *old_name*.

    If the value is obtained from *old_name* a deprecation warning is logged
    once per process lifetime.  Callers should migrate to the new name.
    """
    value = os.environ.get(new_name)
    if value is not None:
        return value

    value = os.environ.get(old_name)
    if value is not None:
        if old_name not in _warned:
            _warned.add(old_name)
            _logger.warning(
                "Deprecated env var '%s' is set. "
                "Please rename it to '%s'. "
                "Support for the old name will be removed in a future release.",
                old_name,
                new_name,
            )
        return value

    return default
