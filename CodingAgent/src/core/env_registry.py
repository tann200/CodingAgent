"""Central registry of all ``CODINGAGENT_*`` environment variables.

This module is the single authoritative source for:

- The canonical name of every env var the agent reads.
- Its legacy alias (``CODING_AGENT_*``), if one exists.
- The type it is parsed as (``str``, ``int``, ``float``, ``bool``).
- The default value used when the variable is absent.
- A human-readable description of what the variable controls.
- The source file(s) where the variable is consumed.

Usage
-----
Import :data:`ENV_REGISTRY` to iterate over all registered variables, or call
:func:`get_entry` to look up a single variable by its canonical name::

    from src.core.env_registry import ENV_REGISTRY, get_entry

    entry = get_entry("CODINGAGENT_SANDBOX_LEVEL")
    print(entry.default)   # "workspace"

For runtime value resolution with automatic legacy-alias fallback, use
:func:`src.core.env_shims.getenv_with_compat` directly.  This module only
describes the variables; it does not read them from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass(frozen=True)
class EnvVarEntry:
    """Metadata record for one environment variable."""

    name: str
    """Canonical ``CODINGAGENT_*`` variable name."""

    description: str
    """Human-readable explanation of what this variable controls."""

    type: str
    """Python type the value is parsed into: ``"str"``, ``"int"``, ``"float"``, or ``"bool"``."""

    default: Any
    """Default value used when the variable is absent or empty."""

    legacy_alias: Optional[str] = None
    """Old ``CODING_AGENT_*`` alias still accepted for backward compatibility.
    ``None`` when no alias exists."""

    sources: List[str] = field(default_factory=list)
    """Source file paths (relative to repo root) that read this variable."""


#: All registered environment variables, ordered alphabetically by canonical name.
ENV_REGISTRY: List[EnvVarEntry] = [
    EnvVarEntry(
        name="CODINGAGENT_ADMIN_TOKEN",
        description=(
            "Bearer token required to access protected HTTP server endpoints "
            "(e.g. /admin/*, /metrics). When unset, admin routes are disabled."
        ),
        type="str",
        default=None,
        legacy_alias="CODING_AGENT_ADMIN_TOKEN",
        sources=[
            "src/server/app.py",
            "src/server/websocket_handler.py",
            "src/core/env_shims.py",
        ],
    ),
    EnvVarEntry(
        name="CODINGAGENT_AUTONOMOUS",
        description=(
            "Set to ``1``, ``true``, or ``yes`` to enable autonomous (non-interactive) "
            "mode at the tools layer, bypassing confirmation prompts that would otherwise "
            "require user approval."
        ),
        type="bool",
        default=False,
        legacy_alias=None,
        sources=["src/tools/tools_config.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_CONTEXT_DIR",
        description=(
            "Name of the per-project context directory created inside the working "
            "directory.  Defaults to ``.codingAgent``.  Override to isolate multiple "
            "agent sessions in the same repo."
        ),
        type="str",
        default=".codingAgent",
        legacy_alias=None,
        sources=[
            "src/main.py",
            "src/core/paths.py",
            "src/core/orchestration/tool_hooks.py",
        ],
    ),
    EnvVarEntry(
        name="CODINGAGENT_DEBUG",
        description=(
            "Enable verbose debug logging to a temp file.  Any non-empty value "
            "activates the ``_dbg()`` helper in ``src/main.py``.  Also accepted "
            "via the legacy alias ``CODING_AGENT_DEBUG``."
        ),
        type="bool",
        default=False,
        legacy_alias="CODING_AGENT_DEBUG",
        sources=["src/main.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_DELEGATION_DEPTH",
        description=(
            "Internal variable injected into subprocess environments to propagate "
            "the current delegation depth across process boundaries.  It is stripped "
            "from the environment before any shell command or tool subprocess runs to "
            "prevent untrusted code from spoofing the depth counter."
        ),
        type="int",
        default=0,
        legacy_alias=None,
        sources=[
            "src/core/orchestration/tool_hooks.py",
            "src/core/orchestration/shell_hooks.py",
        ],
    ),
    EnvVarEntry(
        name="CODINGAGENT_DISTILL_INTERVAL",
        description=(
            "Interval in seconds between automatic memory distillation (history "
            "compaction) runs performed by the background scheduler.  Defaults to "
            "600 seconds (10 minutes)."
        ),
        type="int",
        default=600,
        legacy_alias="CODING_AGENT_DISTILL_INTERVAL",
        sources=["src/core/orchestration/orchestrator_scheduler.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_HTTP_SERVER",
        description=(
            "Set to ``true`` to launch the built-in HTTP/WebSocket server alongside "
            "the agent process.  Disabled by default."
        ),
        type="bool",
        default=False,
        legacy_alias="CODING_AGENT_HTTP_SERVER",
        sources=["src/core/orchestration/orchestrator_services_init.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_LSP_CONTEXT",
        description=(
            "Set to ``1`` to enable Language Server Protocol (LSP) context "
            "augmentation.  When enabled, the context builder enriches prompts with "
            "go-to-definition and symbol information from the LSP server."
        ),
        type="bool",
        default=False,
        legacy_alias=None,
        sources=[
            "src/core/indexing/lsp_context.py",
            "src/core/context/context_builder.py",
        ],
    ),
    EnvVarEntry(
        name="CODINGAGENT_METRICS_AUTH",
        description=(
            "Bearer token required to access the ``/metrics`` endpoint.  When "
            "unset, the metrics endpoint is unauthenticated (relies on network-level "
            "access controls)."
        ),
        type="str",
        default=None,
        legacy_alias="CODING_AGENT_METRICS_AUTH",
        sources=["src/server/app.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_PREFS",
        description=(
            "Path to the user preferences JSON file.  Defaults to the platform "
            "preferences path returned by ``get_prefs_path()``.  Primarily used in "
            "tests to point at a temp file."
        ),
        type="str",
        default=None,  # resolved dynamically via get_prefs_path()
        legacy_alias=None,
        sources=[
            "src/core/user_prefs.py",
            "src/core/inference/adapters/github_copilot_auth.py",
        ],
    ),
    EnvVarEntry(
        name="CODINGAGENT_PREVIEW_RESULT_TTL",
        description=(
            "Time-to-live in seconds for pending diff-gate preview results.  "
            "After this interval the preview is considered stale and the pending "
            "write is abandoned.  Defaults to ``30.0`` seconds."
        ),
        type="float",
        default=30.0,
        legacy_alias=None,
        sources=["src/tools/_diff_gate.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_SANDBOX_LEVEL",
        description=(
            "Bash tool sandbox strictness.  Accepted values: ``off`` (no "
            "restrictions), ``workspace`` (default — confines writes to the working "
            "directory), ``strict`` (block all filesystem mutations).  Can also be "
            "set via the ``--sandbox`` CLI flag."
        ),
        type="str",
        default="workspace",
        legacy_alias=None,
        sources=["src/tools/sandbox.py", "src/main.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_SCHEDULER_HEARTBEAT",
        description=(
            "Interval in seconds between background scheduler heartbeat ticks.  "
            "Controls how frequently the scheduler checks for pending distillation "
            "or other periodic tasks.  Defaults to 60 seconds."
        ),
        type="int",
        default=60,
        legacy_alias="CODING_AGENT_SCHEDULER_HEARTBEAT",
        sources=["src/core/orchestration/orchestrator_scheduler.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_SSE_DROP_POLICY",
        description=(
            "Policy applied when the SSE event queue is full.  "
            "``drop_oldest`` (default) discards the oldest queued event to make "
            "room.  ``drop_newest`` discards the incoming event instead."
        ),
        type="str",
        default="drop_oldest",
        legacy_alias="CODING_AGENT_SSE_DROP_POLICY",
        sources=["src/server/server_config.py", "src/server/websocket_handler.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_SSE_KEEPALIVE",
        description=(
            "Interval in seconds between SSE keep-alive comment frames sent to "
            "connected clients.  Prevents proxies from closing idle connections.  "
            "Defaults to 15 seconds."
        ),
        type="int",
        default=15,
        legacy_alias="CODING_AGENT_SSE_KEEPALIVE",
        sources=["src/server/server_config.py", "src/server/websocket_handler.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_SSE_QUEUE_MAX",
        description=(
            "Maximum number of events buffered in the in-process SSE queue before "
            "the drop policy is applied.  Defaults to 100."
        ),
        type="int",
        default=100,
        legacy_alias="CODING_AGENT_SSE_QUEUE_MAX",
        sources=["src/server/server_config.py", "src/server/websocket_handler.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_STORAGE_BACKEND",
        description=(
            "Storage backend used by ``SessionStore``.  ``sqlite`` (default when "
            "the SQLite driver is available) or ``json`` for a plain-file fallback.  "
            "Leave unset to let the store auto-select the best available backend."
        ),
        type="str",
        default="",
        legacy_alias="CODING_AGENT_STORAGE_BACKEND",
        sources=["src/core/memory/session_store.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_STREAM_TOKENS",
        description=(
            "Set to ``1`` or ``true`` to enable token-by-token streaming from the "
            "LLM.  When disabled, the full response is buffered before being "
            "processed.  Streaming is off by default."
        ),
        type="bool",
        default=False,
        legacy_alias="CODING_AGENT_STREAM_TOKENS",
        sources=["src/core/inference/llm_helpers.py"],
    ),
    EnvVarEntry(
        name="CODINGAGENT_TRUSTED",
        description=(
            "Set to ``1`` to mark the current execution context as trusted, "
            "bypassing the deferred-init safety gate that prevents tool registration "
            "before the orchestrator is fully initialised.  Intended for test "
            "harnesses and CI environments."
        ),
        type="bool",
        default=False,
        legacy_alias=None,
        sources=["src/core/orchestration/deferred_init.py"],
    ),
]

# Build a lookup dict for O(1) access by canonical name.
_REGISTRY_BY_NAME: dict[str, EnvVarEntry] = {e.name: e for e in ENV_REGISTRY}


def get_entry(name: str) -> Optional[EnvVarEntry]:
    """Return the :class:`EnvVarEntry` for *name*, or ``None`` if not registered.

    Args:
        name: The canonical ``CODINGAGENT_*`` variable name.

    Returns:
        The matching entry, or ``None``.
    """
    return _REGISTRY_BY_NAME.get(name)


def all_names() -> List[str]:
    """Return a sorted list of all canonical variable names."""
    return sorted(_REGISTRY_BY_NAME)


def all_legacy_aliases() -> dict[str, str]:
    """Return a mapping of ``legacy_alias -> canonical_name`` for all aliased vars."""
    return {
        e.legacy_alias: e.name
        for e in ENV_REGISTRY
        if e.legacy_alias is not None
    }
