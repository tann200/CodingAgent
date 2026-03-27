"""
Tools configuration module.

Provides configurable values used across tool modules.  Override these by
calling ``configure()`` before using any tool, or set the corresponding
environment variables.

Example::

    from src.tools.tools_config import configure, AGENT_CONTEXT_DIR
    configure(context_dir=".coding-agent-state")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# -----------------------------------------------------------------------
# Module-level state (mutable, not user-facing)
# -----------------------------------------------------------------------

_CONTEXT_DIR: str = ".agent-context"
_DEFAULT_WORKDIR: Optional[Path] = None


# -----------------------------------------------------------------------
# Public helpers
# -----------------------------------------------------------------------


def configure(
    context_dir: str = ".agent-context",
    default_workdir: Optional[Path] = None,
) -> None:
    """Override default tool configuration.

    Call this **once** at startup, before any tool function is invoked.

    Parameters
    ----------
    context_dir:
        Name of the per-project directory used to store tool state
        (TODO.md, TASK_STATE.md, checkpoints, etc.).
        Default: ``".agent-context"``.
    default_workdir:
        Default working directory for tool calls that do not explicitly
        pass ``workdir=``.  When *None* the default is the current working
        directory (``Path.cwd()``).
    """
    global _CONTEXT_DIR, _DEFAULT_WORKDIR
    _CONTEXT_DIR = context_dir
    _DEFAULT_WORKDIR = default_workdir


def agent_context_path(workdir: Path) -> Path:
    """Return the full path to the agent-context directory for *workdir*.

    Creates the directory if it does not exist.
    """
    p = workdir / _CONTEXT_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_default_workdir() -> Path:
    """Return the default working directory."""
    if _DEFAULT_WORKDIR is not None:
        return _DEFAULT_WORKDIR
    return Path.cwd()


def get_context_dir_name() -> str:
    """Return the configured agent-context directory name."""
    return _CONTEXT_DIR
