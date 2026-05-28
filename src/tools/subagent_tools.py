"""
Subagent tools for spawning isolated autonomous agents.

These tools allow the main agent to spawn subagents for specific tasks,
keeping the main agent's context window clean.

When used as a standalone module (without src.core), the delegate_task
function will still work but requires an externally-supplied graph and
system prompt via the `tools_config.configure()` mechanism.
"""

import asyncio
import logging
from contextvars import ContextVar
from typing import Dict, Any, List, Optional, cast
from pathlib import Path

from src.core.paths import get_sessions_dir
from src.tools._tool import tool, PermissionKind
from src.tools.subagent_payloads import (
    build_delegate_result_text,
    build_child_session_file_path,
    build_subagent_initial_state,
    build_subagent_manifest,
    build_subagent_roles_payload,
    build_subagent_session_payload,
    canonicalize_subagent_role,
    compute_effective_tool_policy,
    extract_child_session_messages,
    select_dispatch_result_content,
)


def _get_agent_context_dir(workdir_path: Path) -> Path:
    """Return the canonical agent-context directory for workdir_path (.codingAgent)."""
    try:
        from src.tools.tools_config import agent_context_path as _acp

        resolved = _acp(Path(workdir_path))
        if resolved:
            return resolved
    except Exception:
        pass
    return Path(workdir_path) / ".codingAgent"


def _atomic_write_json(target: Path, obj: dict, logger=None) -> bool:
    """Module-level alias for src.core.io_utils.atomic_write_json.

    Deferred import at call-time preserves test monkeypatching ability.
    """
    from src.core.io_utils import atomic_write_json as _central

    return _central(target, obj, logger=logger)


DispatchEvent: Any = None
DispatchResultEvent: Any = None
DispatchEvents: Any = None
try:
    from src.core.orchestration.event_bus import (
        DispatchEvent,  # type: ignore[assignment]
        DispatchResultEvent,  # type: ignore[assignment]
        DispatchEvents,  # type: ignore[assignment]  # noqa: F401
    )
except ImportError:
    pass

# SPAWN-W1: ContextVar that carries the parent orchestrator reference into tool calls.
# Set by Orchestrator.execute_tool() before dispatching; cleared automatically on exit.
# Tools that spawn subagents (delegate_task) read this to access the full pipeline.
_PARENT_ORCHESTRATOR_VAR: ContextVar[Any] = ContextVar(
    "_parent_orchestrator", default=None
)

# HR-5: Process-local delegation depth counter (not forgeable by subprocesses).
# ContextVar is the authoritative source for in-process depth checks.
# Subprocess inheritance is not supported — subprocesses start at depth 0.
_DELEGATION_DEPTH_VAR: ContextVar[int] = ContextVar("_delegation_depth", default=0)
_MAX_DELEGATION_DEPTH = 3

_get_agent_brain_manager: Any = None
try:
    from src.core.orchestration.agent_brain import get_agent_brain_manager as _get_agent_brain_manager  # type: ignore[assignment]
except ImportError:
    pass


def get_agent_brain_manager() -> Any:
    """Public wrapper around the lazily-imported ``get_agent_brain_manager``.

    Exposes a stable module-level name so tests can monkeypatch
    ``src.tools.subagent_tools.get_agent_brain_manager`` without patching
    the private ``_get_agent_brain_manager`` variable directly.
    """
    if _get_agent_brain_manager is None:
        raise RuntimeError("AgentBrainManager is not available (src.core not importable)")
    return _get_agent_brain_manager()

try:
    from src.core.orchestration.role_config import (
        normalize_role,
        get_role_config,
        is_tool_allowed_for_role,
        CANONICAL_ROLES,
        ROLE_ALIASES,
        CANONICAL_ROLE_CONFIGS,
    )
except ImportError:

    def normalize_role(role: str) -> str:
        """Identity fallback when role_config is not available."""
        return role

    def get_role_config(role: str) -> Optional[Dict[str, Any]]:
        return {}

    def is_tool_allowed_for_role(tool_name: str, role: str) -> bool:
        # QUAL-1: Fail closed — deny all tools when role_config is unavailable
        # rather than silently allowing everything.  This prevents privilege
        # escalation if the import fails due to a misconfiguration or partial install.
        logger.warning(
            "is_tool_allowed_for_role: role_config unavailable — denying tool '%s' for role '%s'",
            tool_name,
            role,
        )
        return False

    CANONICAL_ROLES = ["analyst", "strategic", "operational", "reviewer", "debugger"]
    ROLE_ALIASES = {
        "planner": "strategic",
        "coder": "operational",
        "researcher": "analyst",
    }
    CANONICAL_ROLE_CONFIGS: Dict[str, Any] = {}  # type: ignore[no-redef]


logger = logging.getLogger(__name__)


def _resolve_subagent_graph(*, orchestrator: Any = None, model: Optional[str] = None):
    """Resolve the canonical compiled graph for a subagent run.

    Role validation and role-specific tool policy happen outside graph selection.
    All subagent executions should enter through the same tier-aware compiled
    graph path as the main orchestrator.
    """
    from src.core.orchestration.graph.builder import get_compiled_graph_for_orchestrator

    return get_compiled_graph_for_orchestrator(
        orchestrator=orchestrator,
        model=model,
    )


def _build_valid_roles() -> set:
    """DR-1: Build the set of valid role names at runtime from ROLE_CONFIGS.

    Returns canonical roles + all known aliases so callers can use either form.
    This replaces the previously hard-coded ``valid_roles`` set in
    ``delegate_task`` — the set now tracks ``role_config.py`` automatically.
    """
    roles: set = set(CANONICAL_ROLES)
    roles.update(ROLE_ALIASES.keys())
    return roles


def _build_role_description_block() -> str:
    """DR-1: Build a human-readable role description block from CANONICAL_ROLE_CONFIGS.

    Used in the ``delegate_task`` docstring supplement and in error messages so
    the LLM always receives an accurate, up-to-date role listing.

    Returns a multi-line string like:
        analyst    — Read-only codebase exploration ...
        operational — Implements code changes ...
        ...
    """
    lines = []
    for role in CANONICAL_ROLES:
        cfg = CANONICAL_ROLE_CONFIGS.get(role, {})
        desc = cfg.get("description", "(no description)")
        aliases = [k for k, v in ROLE_ALIASES.items() if v == role]
        alias_str = f"  (aliases: {', '.join(aliases)})" if aliases else ""
        lines.append(f"  {role:<15} — {desc}{alias_str}")
    return "\n".join(lines)


class _MinimalToolRegistry:
    """Minimal tool-registry shim for SubagentOrchestrator.

    Nodes that access ``orchestrator.tool_registry.tools`` or call
    ``orchestrator.tool_registry.get(name)`` will receive an empty but
    valid object instead of raising ``AttributeError: 'NoneType' …``.
    The registry is populated lazily by the graph builder once the real
    ToolRegistry becomes available.
    """

    def __init__(self) -> None:
        self.tools: Dict[str, Any] = {}

    def get(self, name: str) -> Optional[Any]:
        return self.tools.get(name)

    def list(self) -> List[str]:
        return list(self.tools.keys())

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        return []


class SubagentOrchestrator:
    """
    Minimal orchestrator-like object for subagent role enforcement.

    This provides:
    - current_role for tool restriction
    - Basic tool registry
    - No execution (subagent handles its own execution)
    """

    def __init__(
        self,
        role: str,
        working_dir: str,
        allowed_tools: Optional[set] = None,
        denied_tools: Optional[set] = None,
    ):
        self.current_role = normalize_role(role)
        self.working_dir = Path(working_dir)
        self.tool_registry = (
            _MinimalToolRegistry()
        )  # populated by graph builder; never None
        self.cancel_event = None
        self.adapter = None  # perception_node requires orchestrator.adapter (may be None for subagents)
        # SPAWN-W2: per-agent tool allowlist / denylist from AgentDefinition
        self._allowed_tools: Optional[set] = (
            set(allowed_tools) if allowed_tools is not None else None
        )
        self._denied_tools: set = set(denied_tools) if denied_tools else set()

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if tool is allowed for this role + any active allowlist."""
        # SPAWN-W2: allowlist enforcement — reject if outside AgentDefinition.allowed_tools
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            return False
        if tool_name in self._denied_tools:
            return False
        return is_tool_allowed_for_role(tool_name, self.current_role)

    def get_denied_tools(self) -> list:
        """Get list of denied tools for this role."""
        config = get_role_config(self.current_role)
        base_denied: list = []
        if config:
            base_denied = config.get("denied_tools", [])
        return list(set(base_denied) | self._denied_tools)


def _validate_delegate_inputs(role: str, subtask_description: str) -> Optional[str]:
    """Return an error string when inputs are invalid, else None."""
    valid_roles = _build_valid_roles()
    if role not in valid_roles:
        role_block = _build_role_description_block()
        return (
            f"Error: Invalid role '{role}'. Valid roles are:\n{role_block}\n"
            f"Call list_subagent_roles() for an up-to-date listing."
        )
    if not subtask_description or not subtask_description.strip():
        return "Error: subtask_description must not be empty."
    return None


def _resolve_delegate_setup(
    role: str,
    allowed_tools: Optional[list],
    model: Optional[str],
    depth: int,
    workdir_path: "Path",
    parent_orchestrator: Any,
) -> "tuple[str, str, SubagentOrchestrator, Optional[str], Optional[set], set]":
    """Resolve canonical role, system prompt, subagent orchestrator, and tool policy.

    Returns ``(canonical_role, system_prompt, subagent_orchestrator, override_model,
    effective_allowed, effective_denied)``.
    """
    canonical_role = canonicalize_subagent_role(role)
    brain = get_agent_brain_manager()
    system_prompt = brain.compile_system_prompt(canonical_role)

    # SPAWN-W2: Resolve allowed_tools from explicit param or AgentDefinition registry.
    _registry_allowed: Optional[set] = None
    _registry_denied: set = set()
    if allowed_tools is None:
        try:
            from src.core.orchestration.agent_types import get_agent_registry

            _agent_def = get_agent_registry().get(canonical_role)
            if _agent_def is not None:
                _registry_allowed = _agent_def.allowed_tools
                _registry_denied = _agent_def.denied_tools or set()
        except Exception:
            pass

    effective_allowed, effective_denied = compute_effective_tool_policy(
        explicit_allowed_tools=allowed_tools,
        registry_allowed_tools=_registry_allowed,
        registry_denied_tools=_registry_denied,
    )

    subagent_orchestrator = SubagentOrchestrator(
        role=role,
        working_dir=str(workdir_path),
        allowed_tools=effective_allowed,
        denied_tools=effective_denied,
    )

    # SM-1: Resolve override model — explicit param > role default > None
    override_model: Optional[str] = model
    if override_model is None:
        try:
            from src.core.orchestration.role_config import (
                get_default_model_for_role as _gdmfr,
            )

            override_model = _gdmfr(canonical_role)
        except Exception:
            pass

    return canonical_role, system_prompt, subagent_orchestrator, override_model, effective_allowed, effective_denied


def _publish_delegation_start(
    parent_orchestrator: Any,
    child_session_id: str,
    parent_session_id: Optional[str],
    canonical_role: str,
    subtask_description: str,
) -> None:
    """Fire delegation.start event on the parent event bus (best-effort)."""
    try:
        if parent_orchestrator is not None:
            _pbus = getattr(parent_orchestrator, "event_bus", None)
            if _pbus is not None:
                _pbus.publish(
                    "delegation.start",
                    {
                        "child_session_id": child_session_id,
                        "parent_session_id": parent_session_id,
                        "role": canonical_role,
                        "task": subtask_description[:120],
                    },
                )
                if DispatchEvent is not None and hasattr(_pbus, "publish_dispatch"):
                    _dispatch_event = DispatchEvent(
                        session_id=child_session_id,
                        agent_id=canonical_role,
                        task=subtask_description,
                        parent_session_id=parent_session_id,
                    )
                    _pbus.publish_dispatch(_dispatch_event)
    except Exception:
        pass


def _publish_delegation_finish(
    parent_orchestrator: Any,
    child_session_id: str,
    parent_session_id: Optional[str],
    canonical_role: str,
    final_state: Any,
    ok: bool,
) -> None:
    """Fire delegation.finish event and roll up child cost (best-effort)."""
    try:
        if parent_orchestrator is not None:
            _pbus = getattr(parent_orchestrator, "event_bus", None)
            if _pbus is not None:
                _child_cost = (
                    float(final_state.get("session_cost_usd") or 0.0)
                    if final_state and isinstance(final_state, dict)
                    else 0.0
                )
                _pbus.publish(
                    "delegation.finish",
                    {
                        "child_session_id": child_session_id,
                        "role": canonical_role,
                        "ok": ok,
                        "cost_usd": _child_cost if ok else None,
                    },
                )
                if ok and DispatchResultEvent is not None and hasattr(
                    _pbus, "publish_dispatch_result"
                ):
                    _content = select_dispatch_result_content(final_state)
                    _result_event = DispatchResultEvent(
                        session_id=child_session_id,
                        content=_content,
                        parent_session_id=parent_session_id,
                    )
                    _pbus.publish_dispatch_result(_result_event)
                if ok and _child_cost > 0:
                    _pbus.publish(
                        "usage.subagent_cost",
                        {
                            "child_session_id": child_session_id,
                            "role": canonical_role,
                            "cost_usd": _child_cost,
                        },
                    )
    except Exception:
        pass


def _persist_child_session(
    final_state: Any,
    workdir_path: "Path",
    child_session_id: str,
    parent_session_id: Optional[str],
    canonical_role: str,
    subtask_description: str,
    ok: bool,
    error: Optional[str] = None,
) -> None:
    """Write the child session payload to the sessions directory (best-effort)."""
    import time as _time

    try:
        _sessions_dir = get_sessions_dir()
        _sessions_dir.mkdir(parents=True, exist_ok=True)
        _child_msgs = extract_child_session_messages(final_state) if ok else []
        _session_payload = build_subagent_session_payload(
            child_session_id=child_session_id,
            parent_session_id=parent_session_id,
            task_name=subtask_description,
            canonical_role=canonical_role,
            working_dir=str(workdir_path),
            timestamp=_time.time(),
            messages=_child_msgs,
            ok=ok,
            **({"error": error} if error else {}),
        )
        _sp = Path(
            build_child_session_file_path(str(_sessions_dir), child_session_id)
        )
        _atomic_write_json(_sp, _session_payload, logger=logger)
    except Exception:
        pass


@tool(
    side_effects=["execute"], tags=["planning"], permission_kind=PermissionKind.DELEGATE
)
def delegate_task(
    role: str,
    subtask_description: str,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[list] = None,
    model: Optional[str] = None,
    task_id: Optional[str] = None,
) -> str:
    """
    Spawns an isolated autonomous subagent to complete a specific subtask.
    Use this for deep research, isolated debugging, or heavy refactoring
    to keep your own context window clean.

    Args:
        role: The role of the subagent. Call `list_subagent_roles()` to get
              the current list with descriptions. Canonical roles:
                analyst     — deep research / repo exploration (read-only)
                operational — code implementation and edits
                reviewer    — code review and QA
                strategic   — task decomposition and planning
                debugger    — root-cause analysis and fixes
              Legacy aliases (e.g. 'researcher', 'coder', 'planner') are also
              accepted and mapped to their canonical equivalent automatically.
        subtask_description: Highly detailed instructions for the subtask
        working_dir: The directory to execute in (defaults to current directory)
        allowed_tools: Optional explicit list of tool names the subagent may use.
                       When provided, any tool not in this list is rejected.
                       When omitted, the AgentDefinition from the registry for
                       the given role is consulted (SPAWN-W2).
        model: Optional model name to use for this subagent's LLM calls.
               When omitted, the role's default binding from role_config.py or
               the active provider's default model is used.  Use this to pin
               lightweight roles (analyst, reviewer) to a small/fast model while
               keeping operational roles on the frontier model.
               Example: model="gpt-4o-mini"
        task_id: Optional resumption token from a prior delegate_task call.
                 When provided, the subagent attempts to load its saved state
                 from the previous run and continues from where it left off,
                 rather than starting fresh.  The prior run must have completed
                 (or failed) and its state must have been persisted.
                 Obtain this value from the "child_session_id" field in a
                 previous delegate_task result.

    Returns:
        Summary of the subagent's work and final result

    Raises:
        ValueError: If an invalid role is provided
    """
    # DR-1: Validate inputs using helper (checks role validity and non-empty description).
    _input_err = _validate_delegate_inputs(role, subtask_description)
    if _input_err:
        return _input_err

    workdir = working_dir or "."
    workdir_path = Path(workdir).resolve()

    # HR-5 fix: Check delegation depth to prevent unbounded recursive spawning.
    # Use the process-local ContextVar as the authoritative source — it cannot
    # be forged by subprocesses. AgentState["delegation_depth"] is used for
    # cross-session propagation; the ContextVar tracks in-process nesting.
    depth = _DELEGATION_DEPTH_VAR.get()
    if depth >= _MAX_DELEGATION_DEPTH:
        return (
            f"Error: Maximum delegation depth ({_MAX_DELEGATION_DEPTH}) exceeded. "
            f"Refusing to spawn additional subagent to prevent infinite recursion."
        )

    logger.info(
        f"delegate_task: spawning {role} subagent for: {subtask_description[:100]}..."
    )

    try:
        # 1. Setup the isolated initial state
        try:
            (
                canonical_role,
                system_prompt,
                subagent_orchestrator,
                _override_model,
                _effective_allowed,
                _effective_denied,
            ) = _resolve_delegate_setup(
                role=role,
                allowed_tools=allowed_tools,
                model=model,
                depth=depth,
                workdir_path=workdir_path,
                parent_orchestrator=_PARENT_ORCHESTRATOR_VAR.get(None),
            )
        except RuntimeError as _setup_err:
            return f"Error: {_setup_err}"

        # SPAWN-W1: Generate a child session ID and track parent reference.
        # TASK-ID-1: If task_id is provided, use it as session_id so prior state
        # can be loaded for resumption.  Otherwise generate a fresh UUID.
        import uuid as _uuid

        if task_id:
            child_session_id = task_id
        else:
            child_session_id = str(_uuid.uuid4())[:8]
        parent_orchestrator = _PARENT_ORCHESTRATOR_VAR.get(None)
        parent_session_id = None
        if parent_orchestrator is not None:
            parent_session_id = getattr(parent_orchestrator, "_current_task_id", None)

        # TASK-ID-1: Attempt to load prior state when task_id was supplied.
        _resumed_state: Optional[dict] = None
        if task_id:
            try:
                from src.core.memory.session_store import SessionStore as _SS

                _ss_resume = _SS(workdir=str(workdir_path))
                _resumed_state = _ss_resume.load_session_state(task_id)
                if _resumed_state:
                    logger.info(
                        "delegate_task: resuming session %s from saved state "
                        "(%d history msgs)",
                        task_id,
                        len(_resumed_state.get("history", [])),
                    )
            except Exception as _re:
                logger.debug("delegate_task: state resumption failed: %s", _re)
                _resumed_state = None

        initial_state = build_subagent_initial_state(
            subtask_description=subtask_description,
            child_session_id=child_session_id,
            working_dir=str(workdir_path),
            system_prompt=system_prompt,
            current_role=subagent_orchestrator.current_role,
            parent_session_id=parent_session_id,
            delegation_depth=depth + 1,
            override_model=_override_model,
            resumed_state=_resumed_state,
        )

        # SPAWN-W1: Always resolve graphs through the canonical tier-aware builder.
        # When a parent orchestrator exists, inherit its active execution context.
        # Otherwise, resolve against the isolated subagent orchestrator.
        _use_full_pipeline = parent_orchestrator is not None
        _active_orchestrator = subagent_orchestrator
        if _use_full_pipeline:
            try:
                graph = _resolve_subagent_graph(
                    orchestrator=parent_orchestrator,
                    model=_override_model,
                )
                # Set active_agent on parent orchestrator to enforce allowed_tools
                if _effective_allowed is not None or _effective_denied:
                    from src.core.orchestration.agent_types import AgentDefinition

                    _child_agent = AgentDefinition(
                        id=f"delegated_{canonical_role}",
                        name=f"Delegated {canonical_role}",
                        description="Auto-created delegated agent context",
                        allowed_tools=_effective_allowed,
                        denied_tools=_effective_denied,
                    )
                    parent_orchestrator.active_agent = _child_agent
                _active_orchestrator = parent_orchestrator
            except Exception as _e:
                logger.warning(
                    "SPAWN-W1: Failed to use full pipeline: %s; falling back", _e
                )
                _use_full_pipeline = False
        if not _use_full_pipeline:
            try:
                graph = _resolve_subagent_graph(
                    orchestrator=subagent_orchestrator,
                    model=_override_model,
                )
            except Exception as _e:
                return f"Error: Could not create graph for role '{role}': {_e}"
            if graph is None:
                return f"Error: Could not create graph for role '{role}'"

        # 3. Execute the subagent synchronously (blocking until done).
        # Always run in a dedicated thread so we never conflict with an existing event loop.
        # The lambda ensures the coroutine is created inside the worker thread, not on the
        # calling thread (which would be unsafe when passed across threads).
        import concurrent.futures

        def _run_subagent():
            from src.core.orchestration.graph.state import AgentState as _AgentState

            return asyncio.run(
                graph.ainvoke(
                    cast(_AgentState, initial_state),
                    {
                        "configurable": {"orchestrator": _active_orchestrator},
                        "recursion_limit": 50,
                    },
                )
            )

        # CP-2: Write manifest JSON *before* spawning the subagent thread so the
        # parent's context directory always contains a record of the spawned work,
        # even if the subagent crashes or is cancelled.
        import time as _time

        _manifest_dir = _get_agent_context_dir(workdir_path) / "subagent_manifests"
        logger.debug("delegate_task: resolved manifest dir: %s", _manifest_dir)
        _manifest: dict = {}  # initialised here so later update blocks are always bound
        _manifest_path: Optional[Path] = None

        # Use module-level _atomic_write_json helper to write manifests so tests
        # can monkeypatch or replace the central writer. The module-level helper
        # falls back to a local mkstemp+replace implementation when needed.

        try:
            _manifest_dir.mkdir(parents=True, exist_ok=True)
            _manifest = build_subagent_manifest(
                child_session_id=child_session_id,
                parent_session_id=parent_session_id,
                canonical_role=canonical_role,
                task=subtask_description,
                working_dir=str(workdir_path),
                spawned_at=_time.time(),
            )
            _manifest_path = _manifest_dir / f"subagent_{child_session_id}.json"
            if not _atomic_write_json(_manifest_path, _manifest):
                _manifest_path = None
        except Exception as _me:
            logger.exception("delegate_task: manifest write unexpected error: %s", _me)
            _manifest_path = None

        # SUBAGENT-VIS-1: Notify TUI that a subagent is starting
        _publish_delegation_start(
            parent_orchestrator=parent_orchestrator,
            child_session_id=child_session_id,
            parent_session_id=parent_session_id,
            canonical_role=canonical_role,
            subtask_description=subtask_description,
        )

        try:
            # Propagate incremented depth into the child thread's context so that
            # any further in-process delegate_task calls see the correct depth.
            import contextvars as _cv

            # BUG-FIX: ContextVar.set must be called directly, not via ctx.run()
            # Set depth in parent context before copying, then reset it so repeated
            # top-level delegate_task calls do not leak depth into later work/tests.
            _depth_token = _DELEGATION_DEPTH_VAR.set(depth + 1)
            try:
                _child_ctx = _cv.copy_context()
            finally:
                _DELEGATION_DEPTH_VAR.reset(_depth_token)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                # Ensure ContextVars (delegation depth, parent orchestrator, etc.)
                # are visible inside the worker thread by submitting ctx.run.
                future = executor.submit(_child_ctx.run, _run_subagent)
                final_state = future.result(timeout=300)

            # CP-2: Update manifest to completed
            if _manifest_path is not None:
                try:
                    _manifest["status"] = "completed"
                    _manifest["completed_at"] = _time.time()
                    _ok = _atomic_write_json(_manifest_path, _manifest, logger=logger)
                    if not _ok:
                        logger.debug(
                            "delegate_task: atomic_write_json returned False for manifest at %s",
                            _manifest_path,
                        )
                except Exception:
                    pass

            # SUBAGENT-VIS-2: Persist child session to sessions directory
            _persist_child_session(
                final_state=final_state,
                workdir_path=workdir_path,
                child_session_id=child_session_id,
                parent_session_id=parent_session_id,
                canonical_role=canonical_role,
                subtask_description=subtask_description,
                ok=True,
            )

            # SUBAGENT-VIS-1: Notify TUI that subagent finished successfully
            # GAP-NEW-7: also publish subagent cost so parent session can roll it up
            _publish_delegation_finish(
                parent_orchestrator=parent_orchestrator,
                child_session_id=child_session_id,
                parent_session_id=parent_session_id,
                canonical_role=canonical_role,
                final_state=final_state,
                ok=True,
            )

        except Exception as _subagent_err:
            # CP-2: Update manifest to failed
            if _manifest_path is not None:
                try:
                    _manifest["status"] = "failed"
                    _manifest["error"] = str(_subagent_err)
                    _manifest["failed_at"] = _time.time()
                    _ok = _atomic_write_json(_manifest_path, _manifest, logger=logger)
                    if not _ok:
                        logger.debug(
                            "delegate_task: atomic_write_json returned False for manifest at %s",
                            _manifest_path,
                        )
                except Exception:
                    pass

            # SUBAGENT-VIS-2: Persist failed child session skeleton
            _persist_child_session(
                final_state=None,
                workdir_path=workdir_path,
                child_session_id=child_session_id,
                parent_session_id=parent_session_id,
                canonical_role=canonical_role,
                subtask_description=subtask_description,
                ok=False,
                error=str(_subagent_err),
            )

            # SUBAGENT-VIS-1: Notify TUI that subagent failed
            _publish_delegation_finish(
                parent_orchestrator=parent_orchestrator,
                child_session_id=child_session_id,
                parent_session_id=parent_session_id,
                canonical_role=canonical_role,
                final_state=None,
                ok=False,
            )

            raise _subagent_err

        finally:
            # FAULT-07 fix: always reset active_agent regardless of success/failure/exception.
            # Previously this was duplicated in the success and except paths and would leak
            # if an exception was raised before the try block (e.g. during manifest writing).
            if _use_full_pipeline and parent_orchestrator is not None:
                parent_orchestrator.active_agent = None

        # SPAWN-W3: Persist child session in SessionStore so it's queryable later.
        if parent_session_id is not None:
            try:
                from src.core.memory.session_store import SessionStore
                import threading as _thr

                _store = SessionStore(workdir=str(workdir_path))
                # Caller-side instrumentation: log thread and session so diagnostics
                # can correlate which thread attempted the DB write.
                _caller_sid = parent_session_id
                _tname = getattr(_thr.current_thread(), "name", "unknown")
                logger.debug(
                    "session_store: write (session=%r, thread=%s, site=%s)",
                    _caller_sid,
                    _tname,
                    "delegate_task:register_child_session",
                )
                _store.register_child_session(
                    parent_session_id=parent_session_id,
                    child_session_id=child_session_id,
                    role=canonical_role,
                    task=subtask_description,
                )
            except Exception as _se:
                logger.debug(
                    "delegate_task: session store registration failed: %s", _se
                )

        # TASK-ID-1: Persist final state so it can be resumed via task_id later.
        if isinstance(final_state, dict):
            try:
                from src.core.memory.session_store import SessionStore as _SS2
                import threading as _thr

                _ss2 = _SS2(workdir=str(workdir_path))
                # Instrument the caller context for diagnostics
                _caller_sid = child_session_id
                _tname = getattr(_thr.current_thread(), "name", "unknown")
                logger.debug(
                    "session_store: write (session=%r, thread=%s, site=%s)",
                    _caller_sid,
                    _tname,
                    "delegate_task:save_session_state",
                )
                _ss2.save_session_state(
                    session_id=child_session_id,
                    state=final_state,
                    role=canonical_role,
                    task=subtask_description,
                )
            except Exception as _ps_err:
                logger.debug("delegate_task: state persistence failed: %s", _ps_err)

        # 4. Extract and summarize the result
        return build_delegate_result_text(
            role=role,
            child_session_id=child_session_id,
            final_state=final_state,
        )

    except Exception as e:
        logger.error(f"delegate_task: subagent failed: {e}")
        return f"Subagent [{role}] failed during execution: {str(e)}"


@tool(tags=["planning"], permission_kind=PermissionKind.NONE)
def list_subagent_roles() -> Dict[str, Any]:
    """
    List available subagent roles and their purposes.

    Returns:
        Dictionary of available roles and descriptions
    """
    return build_subagent_roles_payload()


async def delegate_task_async(
    role: str,
    subtask_description: str,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[list] = None,
    model: Optional[str] = None,
) -> str:
    """
    Async version of delegate_task for use in async contexts.

    Args:
        role: The role of the subagent
        subtask_description: Detailed instructions for the subtask
        working_dir: The directory to execute in
        allowed_tools: Optional explicit list of tool names the subagent may use.
        model: Optional model name to use for this subagent's LLM calls.

    Returns:
        Summary of the subagent's work
    """
    # Run the sync version in a thread to avoid blocking.
    # max_workers=1: only one task is submitted per executor, no need for an unbounded pool (NEW-16).
    import concurrent.futures

    # HR-12 fix: add timeout to prevent subagent from hanging forever
    _DELEGATION_TIMEOUT_SECONDS = 300.0  # 5 minutes max per subagent

    import contextvars as _cv

    _ctx = _cv.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        # Propagate ContextVars into the subagent thread
        future = executor.submit(
            _ctx.run, delegate_task, role, subtask_description, working_dir, allowed_tools, model
        )
        try:
            return future.result(timeout=_DELEGATION_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return (
                f"Error: Delegation to '{role}' subagent timed out after "
                f"{_DELEGATION_TIMEOUT_SECONDS} seconds. The subagent may be "
                f"hanging or the task is too complex."
            )


# ---------------------------------------------------------------------------
# P2-8: Async parallel subagent delegation
# ---------------------------------------------------------------------------

# Maximum number of subtasks that can be run in parallel in one call.
_PARALLEL_MAX_SUBTASKS: int = 6
# Per-subtask wall-clock timeout in seconds.
_PARALLEL_SUBTASK_TIMEOUT: float = 300.0


@tool(
    side_effects=["execute"],
    tags=["planning", "subagent"],
    permission_kind=PermissionKind.DELEGATE,
)
def delegate_tasks_parallel(
    subtasks: List[Dict[str, Any]],
    working_dir: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Delegate multiple independent subtasks to subagents and run them in parallel.

    Each subtask is run concurrently in its own isolated subagent.  Results are
    collected once all subtasks finish (or time out) and returned as a structured
    summary.

    Use this when you have several *independent* tasks that do not depend on each
    other's output — for example, running analysis on three separate files at the
    same time.  If tasks are sequential or share mutable state, use ``delegate_task``
    instead.

    Args:
        subtasks:
            List of subtask dicts.  Each dict must have:
            - ``role`` (str): Subagent role (e.g. ``"analyst"``, ``"coding"``).
            - ``subtask_description`` (str): Detailed instructions for the subagent.
            Optional per-subtask keys:
            - ``working_dir`` (str): Overrides the top-level ``working_dir``.
            - ``allowed_tools`` (list[str]): Tool allowlist for this subagent.
            - ``model`` (str): Model override for this subagent.
        working_dir:
            Default working directory applied to all subtasks that don't
            specify their own ``working_dir``.
        model:
            Default model applied to all subtasks that don't specify their
            own ``model``.

    Returns:
        Dict with keys:
        - ``ok`` (bool): True if *all* subtasks succeeded.
        - ``results`` (list): Per-subtask result dicts containing ``role``,
          ``subtask_description``, ``ok``, ``output`` (or ``error``).
        - ``succeeded`` (int): Number of successful subtasks.
        - ``failed`` (int): Number of failed/timed-out subtasks.
    """
    import concurrent.futures
    import contextvars as _cv

    if not isinstance(subtasks, list) or not subtasks:
        return {
            "ok": False,
            "error": "subtasks must be a non-empty list of dicts.",
            "results": [],
            "succeeded": 0,
            "failed": 0,
        }

    if len(subtasks) > _PARALLEL_MAX_SUBTASKS:
        return {
            "ok": False,
            "error": (
                f"Too many subtasks: {len(subtasks)} requested, "
                f"max is {_PARALLEL_MAX_SUBTASKS}. Split into multiple calls."
            ),
            "results": [],
            "succeeded": 0,
            "failed": 0,
        }

    # Validate subtask structure up front
    validated: List[Dict[str, Any]] = []
    for i, task in enumerate(subtasks):
        if not isinstance(task, dict):
            return {
                "ok": False,
                "error": f"subtask[{i}] must be a dict, got {type(task).__name__}.",
                "results": [],
                "succeeded": 0,
                "failed": 0,
            }
        role = task.get("role")
        desc = task.get("subtask_description")
        if not role or not isinstance(role, str):
            return {
                "ok": False,
                "error": f"subtask[{i}] missing required string key 'role'.",
                "results": [],
                "succeeded": 0,
                "failed": 0,
            }
        if not desc or not isinstance(desc, str):
            return {
                "ok": False,
                "error": f"subtask[{i}] missing required string key 'subtask_description'.",
                "results": [],
                "succeeded": 0,
                "failed": 0,
            }
        validated.append(
            {
                "role": role,
                "subtask_description": desc,
                "working_dir": task.get("working_dir") or working_dir,
                "allowed_tools": task.get("allowed_tools"),
                "model": task.get("model") or model,
            }
        )

    _ctx = _cv.copy_context()

    def _run_one(task: Dict[str, Any]) -> Dict[str, Any]:
        """Run a single delegate_task call; capture result or error."""
        try:
            output = _ctx.run(
                delegate_task,
                task["role"],
                task["subtask_description"],
                task.get("working_dir"),
                task.get("allowed_tools"),
                task.get("model"),
            )
            ok = not (isinstance(output, str) and output.startswith("Error:"))
            return {
                "role": task["role"],
                "subtask_description": task["subtask_description"],
                "ok": ok,
                "output": output,
            }
        except Exception as exc:
            logger.warning(
                "delegate_tasks_parallel: subtask %r raised %s: %s",
                task["subtask_description"][:60],
                type(exc).__name__,
                exc,
            )
            return {
                "role": task["role"],
                "subtask_description": task["subtask_description"],
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    max_workers = min(len(validated), _PARALLEL_MAX_SUBTASKS)
    results: List[Dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_one, task): task for task in validated}
        for fut in concurrent.futures.as_completed(futures, timeout=_PARALLEL_SUBTASK_TIMEOUT + 10):
            try:
                results.append(fut.result(timeout=1))
            except concurrent.futures.TimeoutError:
                task = futures[fut]
                results.append(
                    {
                        "role": task["role"],
                        "subtask_description": task["subtask_description"],
                        "ok": False,
                        "error": f"Timed out after {_PARALLEL_SUBTASK_TIMEOUT}s.",
                    }
                )
            except Exception as exc:
                task = futures[fut]
                results.append(
                    {
                        "role": task["role"],
                        "subtask_description": task["subtask_description"],
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    succeeded = sum(1 for r in results if r.get("ok"))
    failed = len(results) - succeeded
    return {
        "ok": failed == 0,
        "results": results,
        "succeeded": succeeded,
        "failed": failed,
    }
