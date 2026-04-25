"""Per-project settings loader — CP-13 / CP-8.

Python port of claw-code's 5-layer config system (``config.rs``), scoped to
what CodingAgent needs: project-level and local-override settings files with
deep-merge semantics.

Supported settings file locations (loaded in order, later wins):
    {workdir}/.agent/settings.json        — project settings (committed to VCS)
    {workdir}/.agent/settings.local.json  — local overrides  (gitignored)

Recognised keys (all optional):

    model                    (str)   — LLM model identifier to use for this project
    permissionMode           (str)   — "read_only" | "workspace_write" | "danger_full_access"
                                       | "prompt" | "allow"
    maxTurns                 (int)   — per-session turn limit override
    budgetCeiling            (float) — maximum session spend in USD; triggers usage.budget_exceeded
    hooks                    (dict)  — pre/post-tool-use hook commands (consumed by shell_hooks.py)
    mcpServers               (dict)  — MCP server definitions (future use)
    enableSemanticEvaluation (bool)  — enable WF-2 LLM semantic judge on task completion
                                       (default: True; set false to reduce cost/latency)
    maxLlmWaitSeconds        (int)   — hard timeout in seconds for a single LLM call in
                                       planning_node (default: 120; 0 = disabled)

Usage::

    from src.core.orchestration.project_settings import load_project_settings

    settings = load_project_settings(workdir)
    if settings.permission_mode:
        set_active_permission_mode(settings.permission_mode)
    if settings.model:
        # pass to orchestrator / perception_node …

The loader is **read-only and never raises** — any malformed or missing file
produces a ``ProjectSettings`` with ``None`` / empty-default values.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Candidate settings files, in merge order (last wins for scalars).
def _candidate_settings_files_for_workdir(workdir: Path) -> list[Path]:
    """Return candidate settings files for a workdir, respecting the
    configured context-dir name and falling back to legacy directories.
    """
    try:
        from src.tools.tools_config import get_context_dir_name

        ctx_name = get_context_dir_name()
    except Exception:
        import os as _os

        # Fallback to a safe legacy name when tools_config is unavailable
        ctx_name = _os.getenv("CODINGAGENT_CONTEXT_DIR") or ".agent-context"

    candidate = Path(ctx_name)
    # The caller will resolve relative to the provided base workdir.
    return [candidate / "settings.json", candidate / "settings.local.json"]


# Mapping from the JSON ``permissionMode`` string to CodingAgent's
# ``PermissionLevel`` enum string value.  Imported lazily to avoid circular
# imports.
_PERMISSION_MODE_MAP: dict[str, str] = {
    "read_only": "read_only",
    "readOnly": "read_only",
    "ReadOnly": "read_only",
    "workspace_write": "workspace_write",
    "workspaceWrite": "workspace_write",
    "WorkspaceWrite": "workspace_write",
    "danger_full_access": "danger",
    "dangerFullAccess": "danger",
    "DangerFullAccess": "danger",
    "prompt": "prompt",
    "Prompt": "prompt",
    "allow": "allow",
    "Allow": "allow",
}


# ---------------------------------------------------------------------------
# Module-level singleton — set by main.py after loading
# ---------------------------------------------------------------------------

#: The active ``ProjectSettings`` instance for the current session.
#: ``None`` until ``load_project_settings()`` has been called by startup code.
_ACTIVE_SETTINGS: Optional["ProjectSettings"] = None


def get_active_settings() -> "ProjectSettings | None":
    """Return the currently active project settings, or *None* if not yet loaded."""
    return _ACTIVE_SETTINGS


@dataclass
class ProjectSettings:
    """Resolved per-project settings after loading and merging all files."""

    model: Optional[str] = None
    """LLM model override for this project (e.g. ``"gpt-4o"``)."""

    permission_mode: Optional[str] = None
    """Resolved ``PermissionLevel`` string value, or *None* if not set."""

    max_turns: Optional[int] = None
    """Per-session maximum turn count override."""

    hooks: dict[str, list[str]] = field(default_factory=dict)
    """Pre/post-tool-use hook command lists (consumed by ``ShellHookRunner``)."""

    mcp_servers: dict[str, Any] = field(default_factory=dict)
    """MCP server definitions (future use)."""

    budget_ceiling_usd: Optional[float] = None
    """Maximum session spend in USD.  When set, the ``SessionCostTracker`` will
    publish a ``usage.budget_exceeded`` event (and log a warning) the first time
    the cumulative session cost exceeds this value."""

    enable_semantic_evaluation: bool = True
    """PERF-2: Whether to fire the WF-2 LLM semantic judge on task completion.
    Defaults to ``True``.  Set to ``False`` (``enableSemanticEvaluation: false``
    in settings.json) to skip the extra LLM call on cost-sensitive deployments."""

    max_llm_wait_seconds: int = 120
    """WF-5: Hard timeout (seconds) for a single LLM call in planning_node.
    When the call exceeds this duration it is cancelled and the node routes to
    ``wait_for_user`` with an error message.  Set to ``0`` to disable the timeout."""

    raw: dict[str, Any] = field(default_factory=dict)
    """The final deep-merged raw settings dict (for forward compatibility)."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_project_settings(workdir: str | Path | None = None) -> ProjectSettings:
    """Load and merge project settings from ``{workdir}/.agent/settings*.json``.

    Parameters
    ----------
    workdir:
        Project working directory.  Defaults to ``Path.cwd()``.

    Returns
    -------
    ProjectSettings
        Always returns a valid object — never raises.
    """
    base = Path(workdir) if workdir else Path.cwd()
    merged: dict[str, Any] = {}

    # Resolve candidate settings files for this workdir and also try legacy
    # directories if the configured candidate does not exist.
    candidates = _candidate_settings_files_for_workdir(base)
    # If the configured candidate doesn't exist, prefer legacy directories
    # (".agent", ".agent-context") that may already be present in older
    # workspaces.
    resolved: list[Path] = []
    for rel in candidates:
        candidate_path = base / rel
        resolved.append(candidate_path)

    # Legacy fallbacks
    for legacy_name in (".agent-context", ".agent"):
        legacy_base = base / legacy_name
        resolved.append(legacy_base / "settings.json")
        resolved.append(legacy_base / "settings.local.json")

    # Iterate in order and merge any existing JSON settings
    for path in resolved:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                logger.debug(
                    "project_settings: %s is not a JSON object, skipping", path
                )
                continue
            _deep_merge(merged, data)
            logger.debug("project_settings: loaded %s", path)
        except Exception as exc:
            logger.debug("project_settings: failed to load %s: %s", path, exc)

    return _parse(merged)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Recursively merge *override* into *base* in-place.

    - Nested dicts are merged recursively.
    - All other types use last-write-wins (``override`` wins).
    """
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


def _parse(raw: dict[str, Any]) -> ProjectSettings:
    """Convert a merged raw settings dict into a ``ProjectSettings`` instance."""
    # model
    model: Optional[str] = None
    raw_model = raw.get("model")
    if isinstance(raw_model, str) and raw_model.strip():
        model = raw_model.strip()

    # permissionMode
    permission_mode: Optional[str] = None
    raw_pm = raw.get("permissionMode") or raw.get("permission_mode")
    if isinstance(raw_pm, str):
        mapped = _PERMISSION_MODE_MAP.get(raw_pm.strip())
        if mapped:
            permission_mode = mapped
        else:
            logger.debug(
                "project_settings: unknown permissionMode %r, ignoring", raw_pm
            )

    # maxTurns
    max_turns: Optional[int] = None
    raw_mt = raw.get("maxTurns") or raw.get("max_turns")
    if raw_mt is not None:
        try:
            max_turns = int(raw_mt)
        except (TypeError, ValueError):
            logger.debug("project_settings: invalid maxTurns %r, ignoring", raw_mt)

    # hooks — keep as-is for ShellHookRunner
    hooks: dict[str, list[str]] = {}
    raw_hooks = raw.get("hooks")
    if isinstance(raw_hooks, dict):
        for event_name in ("PreToolUse", "PostToolUse"):
            cmds = raw_hooks.get(event_name)
            if isinstance(cmds, list):
                hooks[event_name] = [str(c) for c in cmds]

    # mcpServers
    mcp_servers: dict[str, Any] = {}
    raw_mcp = raw.get("mcpServers")
    if isinstance(raw_mcp, dict):
        mcp_servers = raw_mcp

    # budgetCeiling (CAP-1)
    budget_ceiling_usd: Optional[float] = None
    raw_bc = raw.get("budgetCeiling") or raw.get("budget_ceiling_usd")
    if raw_bc is not None:
        try:
            budget_ceiling_usd = float(raw_bc)
        except (TypeError, ValueError):
            logger.debug("project_settings: invalid budgetCeiling %r, ignoring", raw_bc)

    # enableSemanticEvaluation (PERF-2)
    enable_semantic_evaluation: bool = True
    raw_ese = raw.get("enableSemanticEvaluation")
    if raw_ese is None:
        raw_ese = raw.get("enable_semantic_evaluation")
    if raw_ese is not None:
        if isinstance(raw_ese, bool):
            enable_semantic_evaluation = raw_ese
        elif isinstance(raw_ese, str):
            enable_semantic_evaluation = raw_ese.lower() not in ("false", "0", "no")
        else:
            enable_semantic_evaluation = bool(raw_ese)

    # maxLlmWaitSeconds (WF-5)
    max_llm_wait_seconds: int = 120
    raw_mlw = raw.get("maxLlmWaitSeconds")
    if raw_mlw is None:
        raw_mlw = raw.get("max_llm_wait_seconds")
    if raw_mlw is not None:
        try:
            max_llm_wait_seconds = int(raw_mlw)
        except (TypeError, ValueError):
            logger.debug(
                "project_settings: invalid maxLlmWaitSeconds %r, ignoring", raw_mlw
            )

    return ProjectSettings(
        model=model,
        permission_mode=permission_mode,
        max_turns=max_turns,
        hooks=hooks,
        mcp_servers=mcp_servers,
        budget_ceiling_usd=budget_ceiling_usd,
        enable_semantic_evaluation=enable_semantic_evaluation,
        max_llm_wait_seconds=max_llm_wait_seconds,
        raw=raw,
    )
