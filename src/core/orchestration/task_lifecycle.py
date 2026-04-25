"""Task lifecycle helpers extracted from Orchestrator (Phase D refactor).

These module-level functions accept an ``orch`` (Orchestrator) instance as
their first argument so that ``self.X`` → ``orch.X`` is a mechanical 1-to-1
substitution and no mutation issues arise.

The Orchestrator delegates to these functions; callers that imported from
``orchestrator`` are unaffected because the Orchestrator methods still exist
as thin wrappers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.logger import logger as guilogger


# ---------------------------------------------------------------------------
# start_new_task
# ---------------------------------------------------------------------------


def start_new_task_impl(orch) -> str:
    """
    Start a new task by generating a new task ID and clearing per-task state.
    Returns the new task ID.

    ME-2 fix: previously only msg_mgr.messages was cleared; the session read/modified
    file sets and rollback snapshots were left over from the prior task, which could
    cause incorrect read-before-edit guards and stale rollback state.

    GAP 1: Also updates AgentSessionManager for state hydration.
    """
    import uuid

    orch._current_task_id = str(uuid.uuid4())[:8]
    try:
        orch.msg_mgr.messages = []
    except Exception:
        pass
    # Reset per-task session tracking
    orch._session_read_files = set()
    orch._session_modified_files = set()

    # P2-C: Pre-populate _session_read_files with agent-internal files so that
    # the read-before-write guard never blocks the agent from updating them.
    # These files are written by the agent itself (manage_todo, planning_node,
    # memory_sync) and their content is always injected into the system prompt
    # by ContextBuilder — the agent therefore "knows" them without an explicit
    # read_file call.
    try:
        _wd = Path(orch.working_dir) if orch.working_dir else Path.cwd()
        try:
            from src.tools.tools_config import agent_context_path

            canonical_ctx = Path(agent_context_path(_wd))
        except Exception:
            # Fallback to legacy name when tools_config isn't importable
            canonical_ctx = _wd / ".agent-context"

        # Also consider legacy/alternate context directories to ensure tests
        # that expect legacy ".agent-context" paths are satisfied even when
        # a configured directory (e.g. ".localAgent") exists on disk.
        legacy_a = _wd / ".agent-context"
        legacy_b = _wd / ".agent"

        # Build an ordered, deduplicated list of candidate context dirs to seed
        dirs_to_seed: list[Path] = []
        for d in (canonical_ctx, legacy_a, legacy_b):
            if d not in dirs_to_seed:
                dirs_to_seed.append(d)

        _internal_files: list[Path] = []
        for ctx in dirs_to_seed:
            _internal_files.extend(
                [
                    ctx / "TODO.md",
                    ctx / "todo.json",
                    ctx / "PLAN.md",
                    ctx / "state.json",
                    ctx / "TASK_STATE.md",
                ]
            )

        for _f in _internal_files:
            # Use resolved absolute paths to match test expectations (realpath)
            try:
                orch._session_read_files.add(str(_f.resolve()))
            except Exception:
                # If resolution fails, fall back to the string path
                orch._session_read_files.add(str(_f))
    except Exception:
        pass
    # Reset read-before-write guardrail state for the new task
    try:
        from src.tools.guardrails import reset_guardrail_state

        reset_guardrail_state()
    except Exception:
        pass
    # Reset execution trace buffer for the new task
    orch._execution_trace_buffer = []  # type: ignore[attr-defined]
    # Reset rollback manager snapshot state
    try:
        if hasattr(orch, "rollback_manager"):
            orch.rollback_manager.current_snapshot = None
            orch._current_snapshot_id = None
            orch._step_snapshot_id = None
    except Exception:
        pass

    # Reset plan mode state for the new task
    if hasattr(orch, "plan_mode") and orch.plan_mode:
        orch.plan_mode.disable()
    orch._plan_approval_event = None
    orch._plan_approved = False

    # HR-9 fix: clear stale delegations from the previous task so that
    # memory_sync does not spuriously route to delegation_node on the next run.
    if hasattr(orch, "_pending_delegations"):
        orch._pending_delegations = []

    # MM-1 fix: Invalidate compaction checkpoint from previous task to
    # prevent cross-task memory contamination.
    try:
        try:
            from src.tools.tools_config import agent_context_path

            _cp = (
                agent_context_path(Path(orch.working_dir)) / "compaction_checkpoint.md"
            )
        except Exception:
            _cp = Path(orch.working_dir) / ".agent-context" / "compaction_checkpoint.md"
        if _cp.exists():
            _cp.unlink()
    except Exception:
        pass

    # P2-C: Reset compaction cooldown fields so the new task starts fresh.
    if hasattr(orch, "_state") and isinstance(orch._state, dict):
        orch._state["_compacted_history"] = None
        orch._state["_compaction_last_round"] = None

    # CROSS-SESSION FIX: Delete TODO.md and TASK_STATE.md at the start of each
    # new task so ContextBuilder does not inject the previous task's plan as
    # <task_progress> into the round-0 system prompt.
    #
    # Without this, a stale TODO.md from "add hello-world function" is read by
    # ContextBuilder._get_todo_content() and injected verbatim into the system
    # message for the very next task ("summarize the project readme"), causing
    # the agent to follow the old plan (list files → read main.py) instead of
    # the new one (read README.md).
    #
    # planning_node recreates TODO.md as soon as it produces a new plan, so
    # deleting it here only removes the window where stale content is visible.
    try:
        # Prefer using manage_todo to clear TODO files so RBW/session state and
        # ContextBuilder caches are updated consistently. Falling back to direct
        # unlink if manage_todo is unavailable.
        try:
            from src.tools.todo_tools import manage_todo

            manage_todo(action="clear", workdir=str(orch.working_dir))
            guilogger.info("start_new_task: cleared TODO via manage_todo")
        except Exception:
            _agent_ctx = Path(orch.working_dir) / ".agent-context"
            _p = _agent_ctx / "TODO.md"
            if _p.exists():
                try:
                    _p.unlink()
                    guilogger.info(
                        "start_new_task: deleted stale TODO.md to prevent cross-session prompt contamination"
                    )
                except Exception:
                    pass
        # TASK_STATE.md still needs explicit deletion
        try:
            try:
                from src.tools.tools_config import agent_context_path

                _agent_ctx = agent_context_path(Path(orch.working_dir))
            except Exception:
                _agent_ctx = Path(orch.working_dir) / ".agent-context"
            _ts = _agent_ctx / "TASK_STATE.md"
            if _ts.exists():
                _ts.unlink()
                guilogger.info(
                    "start_new_task: deleted stale TASK_STATE.md to prevent cross-session prompt contamination"
                )
        except Exception:
            pass
    except Exception:
        pass

    # PB-2 fix: Invalidate ContextBuilder module-level file caches so that
    # role/skill YAML files modified on disk between tasks are re-read rather
    # than served from a process-global stale cache entry.
    try:
        from src.core.context.context_builder import ContextBuilder

        ContextBuilder.clear_cache()
    except Exception:
        pass

    # UP-2 fix: Evict stale repo summary cache for this working directory so
    # that analysis_node serves a fresh summary on the next task.  Files may
    # have changed since the last task (agent wrote/deleted files), and the
    # cached summary would otherwise be served until the process restarts.
    try:
        from src.core.orchestration.graph.nodes.analysis_node import (
            clear_repo_summary_cache,
        )

        _wd_str = str(orch.working_dir) if orch.working_dir else None
        clear_repo_summary_cache(working_dir=_wd_str)
    except Exception:
        pass

    # HR-8 fix: clear stale preview events so wait_for_user_node does not
    # block on a preview that was never confirmed/rejected in the last task.
    try:
        from src.core.orchestration.preview_service import PreviewService

        svc = PreviewService.get_instance()
        svc.pending_previews.clear()
    except Exception:
        pass

    # GAP 1: Update AgentSessionManager for state hydration
    try:
        from src.core.orchestration.agent_session_manager import (
            get_agent_session_manager,
        )

        session_mgr = get_agent_session_manager()
        session_mgr.update_session_state(
            session_id=orch._current_task_id,
            task="",
            message_history=[],
            current_plan=[],
            current_step=0,
            provider=orch._provider_name or "",
            model=getattr(orch, "_model", ""),
            files_read=[],
            files_modified=[],
        )
    except Exception as e:
        guilogger.debug(f"Failed to update session manager: {e}")

    # CF-3: Reset GAP-SMALL-4 clarification flag so a stale True from the
    # previous task doesn't immediately short-circuit route_after_perception.
    try:
        if hasattr(orch, "_last_agent_state") and isinstance(
            orch._last_agent_state, dict
        ):
            orch._last_agent_state["needs_clarification"] = None
    except Exception:
        pass

    # ORCH-W5: Reset session title so the next task gets its own generated title.
    orch._session_title = None
    # ORCH-W4: Reset agent mode to "execution" for the new task.
    orch._agent_mode = "execution"

    # OP-5: Apply per-project tool enable/disable overrides from
    # .agent-context/config.json so project-specific tool restrictions
    # are enforced at the start of each task.
    try:
        from src.core.config_loader import get_project_tool_overrides

        _wd_str = str(orch.working_dir) if orch.working_dir else ""
        if _wd_str and hasattr(orch, "tool_registry"):
            _tool_overrides = get_project_tool_overrides(_wd_str)
            for _tool_name, _enabled in _tool_overrides.items():
                if not _enabled:
                    try:
                        orch.tool_registry.deregister(_tool_name)
                        guilogger.info(
                            "start_new_task: disabled tool '%s' (project config)",
                            _tool_name,
                        )
                    except Exception:
                        pass
    except Exception:
        pass

    guilogger.info(f"Started new task with ID: {orch._current_task_id}")
    # TUI-07: publish git status so the sidebar branch chip is current
    orch._publish_git_status()
    return orch._current_task_id


# ---------------------------------------------------------------------------
# restore_continue_state
# ---------------------------------------------------------------------------


def restore_continue_state_impl(orch, state: dict) -> None:
    """Restore saved conversation state for the /continue workflow.

    Accepts the dict produced by the TUI's _save_state_for_continue and applies
    it to the orchestrator without the caller needing to touch private fields.
    """
    try:
        if hasattr(orch, "msg_mgr") and "history" in state:
            history = state["history"]
            if history and hasattr(orch.msg_mgr, "messages"):
                orch.msg_mgr.messages = list(history)
        if "session_read_files" in state:
            orch._session_read_files = set(state["session_read_files"])
        # Restore AgentState fields used for mid-plan resume (U6)
        last_state = getattr(orch, "_last_agent_state", None) or {}
        for key in (
            "current_plan",
            "current_step",
            "working_dir",
            "step_retry_counts",
        ):
            val = state.get(key)
            if val is not None:
                last_state[key] = val
        orch._last_agent_state = last_state
    except Exception as e:
        guilogger.error(f"restore_continue_state failed: {e}")


# ---------------------------------------------------------------------------
# _sync_session_state
# ---------------------------------------------------------------------------


def sync_session_state_impl(orch) -> None:
    """Sync current orchestrator state to AgentSessionManager for hydration.

    Delegates to :meth:`SessionManager.sync_agent_session_state`.
    """
    if not hasattr(orch, "session_mgr"):
        return
    orch.session_mgr.sync_agent_session_state(
        adapter=getattr(orch, "_adapter", None),
    )


# ---------------------------------------------------------------------------
# get_current_task_id
# ---------------------------------------------------------------------------


def get_current_task_id_impl(orch) -> Optional[str]:
    """Get the current task ID."""
    return orch._current_task_id


# ---------------------------------------------------------------------------
# get_file_lock_manager
# ---------------------------------------------------------------------------


def get_file_lock_manager_impl(orch):
    """Get the FileLockManager for PRSW coordination."""
    return orch.file_lock_manager


# ---------------------------------------------------------------------------
# approve_plan
# ---------------------------------------------------------------------------


def approve_plan_impl(orch) -> None:
    """Called by TUI when user approves the pending plan."""
    orch._plan_approved = True
    if orch.plan_mode:
        orch.plan_mode.disable()
    if orch._plan_approval_event:
        orch._plan_approval_event.set()
    guilogger.info("Orchestrator: plan approved by user")


# ---------------------------------------------------------------------------
# reject_plan
# ---------------------------------------------------------------------------


def reject_plan_impl(orch) -> None:
    """Called by TUI when user rejects the pending plan."""
    orch._plan_approved = False
    if orch._plan_approval_event:
        orch._plan_approval_event.set()
    guilogger.info("Orchestrator: plan rejected by user")


# ---------------------------------------------------------------------------
# wait_for_plan_approval
# ---------------------------------------------------------------------------


async def wait_for_plan_approval_impl(orch) -> bool:
    """Suspend until user approves or rejects the plan. Returns True if approved."""
    orch._plan_approval_event = asyncio.Event()
    orch._plan_approved = False
    await orch._plan_approval_event.wait()
    return orch._plan_approved


# ---------------------------------------------------------------------------
# get_tools_for_role
# ---------------------------------------------------------------------------


def get_tools_for_role_impl(orch, role: str) -> List[Dict[str, Any]]:
    """Return the tool list filtered to the toolset appropriate for *role*.

    OE-5 fix: ToolsetManager existed but was never wired; every node passed
    the full registry (~40 tools) to the LLM regardless of what the role
    actually needs.  This method returns only the tools in the matching
    toolset, falling back to the full registry when the toolset is unknown or
    its tools are not all registered (avoids silently stripping required tools).

    Usage in nodes::

        tools_list = orchestrator.get_tools_for_role("debugger")
        # → filtered to debug toolset: bash, read_file, edit_file, run_tests …
    """
    try:
        # Import the toolset loader module (prefer src.tools.toolsets.loader,
        # fall back to src.config.toolsets.loader). We import the module so we
        # can use model-aware loading when available (load_toolset_for_model).
        try:
            import importlib

            ts_loader = importlib.import_module("src.tools.toolsets.loader")
        except Exception:
            import importlib

            ts_loader = importlib.import_module("src.config.toolsets.loader")

        toolset_name = ts_loader.get_toolset_for_role(role)

        # If the loader exposes a model-aware loader, prefer it so the
        # file-format selection can be model-dependent. Fall back to the
        # traditional get_tools_for_toolset when the helper is missing or
        # returns nothing useful.
        model = getattr(orch, "_model", None)
        toolset_tool_names = []
        if hasattr(ts_loader, "load_toolset_for_model"):
            try:
                ts = ts_loader.load_toolset_for_model(toolset_name, model)
                if ts and "tools" in ts:
                    toolset_tool_names = list(ts["tools"])
            except Exception:
                toolset_tool_names = []

        if not toolset_tool_names:
            toolset_tool_names = ts_loader.get_tools_for_toolset(toolset_name)
        if not toolset_tool_names:
            # Unknown toolset — fall back to full registry
            raise ValueError(f"empty toolset for role={role!r}")
        all_tools = orch.tool_registry.tools
        # Include only tools that are actually registered
        filtered = [
            {"name": n, "description": all_tools[n].get("description", "")}
            for n in toolset_tool_names
            if n in all_tools
        ]
        # Safety: if fewer than 3 tools matched, fall back to full list
        # (toolset YAML may be stale relative to the registry)
        if len(filtered) < 3:
            raise ValueError(
                f"toolset {toolset_name!r} matched too few registered tools"
            )
        return filtered
    except Exception as _e:
        # SCAN-4 fix: log a warning so operators can detect toolset misconfiguration.
        # The graceful fallback to the full registry is intentional (nodes must always
        # receive a usable tool list), but silent failures mask YAML staleness issues.
        guilogger.warning(
            f"get_tools_for_role({role!r}): toolset lookup failed ({_e}); "
            "falling back to full tool registry"
        )
        return [
            {"name": n, "description": m.get("description", "")}
            for n, m in orch.tool_registry.tools.items()
        ]
