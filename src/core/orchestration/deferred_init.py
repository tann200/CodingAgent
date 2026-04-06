"""deferred_init.py — Deferred feature gating for the orchestrator.

TASK-11: Provides ``DeferredInitResult`` and ``run_deferred_init()`` so that
security-sensitive subsystems (MCP server, hook runner, plugin loading) are
only activated when the workspace is explicitly trusted.

This mirrors the pattern from ``claw-code-main``'s ``deferred_init.py``:
``run_deferred_init.startup_steps()`` gates subsystem start on the *trusted*
flag, which the orchestrator sets based on user consent or the
``CODINGAGENT_TRUSTED`` environment variable.

Usage::

    from src.core.orchestration.deferred_init import run_deferred_init

    result = await run_deferred_init(orchestrator, trusted=True)
    if result.mcp_started:
        logger.info("MCP server is running")
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat as _stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass  # avoid circular imports

logger = logging.getLogger(__name__)

_TRUSTED_ENV_VAR = "CODINGAGENT_TRUSTED"


@dataclass
class DeferredInitResult:
    """Records which subsystems were started during deferred init.

    Attributes:
        mcp_started:    Whether the MCP stdio server was launched.
        hooks_enabled:  Whether pre/post tool hooks are active.
        plugins_loaded: Number of plugin modules loaded.
        errors:         List of non-fatal errors encountered during init.
    """

    mcp_started: bool = False
    hooks_enabled: bool = False
    plugins_loaded: int = 0
    errors: list[str] = field(default_factory=list)


def is_trusted(orchestrator: Any | None = None) -> bool:
    """Return True if the current workspace should be treated as trusted.

    Priority:
    1. ``orchestrator.trusted`` attribute (set explicitly at construction).
    2. ``CODINGAGENT_TRUSTED=1`` environment variable.
    3. Default: False (safe).
    """
    if orchestrator is not None and getattr(orchestrator, "trusted", None) is not None:
        return bool(orchestrator.trusted)
    env = os.environ.get(_TRUSTED_ENV_VAR, "").strip().lower()
    return env in ("1", "true", "yes")


async def run_deferred_init(
    orchestrator: Any,
    trusted: bool | None = None,
) -> DeferredInitResult:
    """Run deferred initialisation of security-gated subsystems.

    Args:
        orchestrator: The ``Orchestrator`` instance (may be None in tests).
        trusted:      Override the trust determination.  If None, calls
                      ``is_trusted(orchestrator)`` to determine the flag.

    Returns:
        A ``DeferredInitResult`` describing what was started.
    """
    result = DeferredInitResult()

    if trusted is None:
        trusted = is_trusted(orchestrator)

    if not trusted:
        logger.info(
            "deferred_init: workspace not trusted — "
            "MCP server, hooks, and plugins are disabled. "
            "Set %s=1 or pass trusted=True to enable.",
            _TRUSTED_ENV_VAR,
        )
        return result

    logger.info("deferred_init: trusted workspace — starting optional subsystems")

    # ── 1. MCP stdio server ──────────────────────────────────────────────────
    try:
        mcp_server = getattr(orchestrator, "mcp_server", None)
        if mcp_server is not None and hasattr(mcp_server, "start"):
            await mcp_server.start()
            result.mcp_started = True
            logger.info("deferred_init: MCP server started")
    except Exception as exc:
        msg = f"MCP server start failed: {exc}"
        result.errors.append(msg)
        logger.warning("deferred_init: %s", msg)

    # ── 2. Tool hooks ────────────────────────────────────────────────────────
    try:
        hook_runner = getattr(orchestrator, "_tool_hook_runner", None)
        if hook_runner is not None:
            # Hooks are enabled by default when the hook runner is present;
            # just mark them as active here so the result reflects reality.
            result.hooks_enabled = True
            logger.info("deferred_init: tool hooks enabled")
    except Exception as exc:
        msg = f"Hook runner init failed: {exc}"
        result.errors.append(msg)
        logger.warning("deferred_init: %s", msg)

    # ── 3. Plugin loading ────────────────────────────────────────────────────
    try:
        plugin_dir = getattr(orchestrator, "plugin_dir", None)
        if plugin_dir is not None:
            from pathlib import Path

            _dir = Path(plugin_dir)
            if _dir.is_dir():
                import importlib.util, sys

                # Security: refuse to load plugins from world-writable directories.
                # Check both the symlink entry itself (lstat) and its target (stat)
                # so that world-writable symlinks pointing to safe targets are also
                # caught.
                try:
                    import os as _os

                    _target_stat = _dir.stat()  # follows symlinks → checks target
                    _link_stat = _os.lstat(
                        _dir
                    )  # does NOT follow → checks symlink itself
                    if (_target_stat.st_mode & _stat.S_IWOTH) or (
                        _link_stat.st_mode & _stat.S_IWOTH
                    ):
                        logger.error(
                            "deferred_init: plugin_dir '%s' is world-writable — "
                            "refusing to load plugins. Fix permissions with: chmod o-w '%s'",
                            _dir,
                            _dir,
                        )
                        result.errors.append(
                            f"plugin_dir is world-writable (skipped): {_dir}"
                        )
                        plugin_dir = None  # skip loading loop below
                except OSError as perm_exc:
                    logger.warning(
                        "deferred_init: could not stat plugin_dir '%s': %s",
                        _dir,
                        perm_exc,
                    )
                    plugin_dir = None

                if plugin_dir is None:
                    return result

                count = 0
                for plugin_path in sorted(_dir.glob("*.py")):
                    if plugin_path.stem.startswith("_"):
                        continue
                    try:
                        spec = importlib.util.spec_from_file_location(
                            f"codingagent.plugins.{plugin_path.stem}", plugin_path
                        )
                        if spec and spec.loader:
                            mod = importlib.util.module_from_spec(spec)
                            sys.modules[spec.name] = mod
                            spec.loader.exec_module(mod)  # type: ignore[union-attr]
                            count += 1
                    except Exception as plugin_exc:
                        result.errors.append(f"Plugin {plugin_path.name}: {plugin_exc}")
                        logger.warning(
                            "deferred_init: plugin load error: %s", plugin_exc
                        )
                result.plugins_loaded = count
                if count:
                    logger.info("deferred_init: loaded %d plugin(s)", count)
    except Exception as exc:
        msg = f"Plugin loading failed: {exc}"
        result.errors.append(msg)
        logger.warning("deferred_init: %s", msg)

    return result
