"""Initial-state and turn-budget helpers for the inference loop."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from src.tools.subagent_payloads import build_graph_state_base


def build_initial_state(
    *,
    orch: Any,
    prompt: str,
    full_system_prompt: str,
    cancel_event: Any,
) -> Dict[str, Any]:
    """Build the initial graph state for one agent run."""
    return {
        **build_graph_state_base(
            task=prompt,
            session_id=orch._current_task_id,
            working_dir=str(orch.working_dir),
            system_prompt=full_system_prompt,
            history=orch.msg_mgr.messages,
            verified_reads=list(orch._session_read_files),
            parent_session_id=None,
            delegation_depth=0,
        ),
        "deterministic": getattr(orch, "deterministic", False),
        "cancel_event": cancel_event,
        "seed": getattr(orch, "seed", None),
        "empty_response_count": 0,
        "analysis_summary": None,
        "relevant_files": [],
        "key_symbols": [],
        "repo_summary_data": None,
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "total_debug_attempts": 0,
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": True,
        "task_decomposed": False,
        "tool_call_count": 0,
        "max_tool_calls": 30,
        "tool_last_used": {},
        "files_read": {},
        "analyst_findings": None,
        "plan_resumed": False,
        "plan_mode_enabled": getattr(getattr(orch, "plan_mode", None), "enabled", False),
        "awaiting_plan_approval": False,
        "plan_mode_approved": None,
        "plan_mode_blocked_tool": None,
        "needs_clarification": None,
        "_file_lock_manager": getattr(orch, "file_lock_manager", None),
        "_write_queue": [],
        "_agent_session_manager": getattr(orch, "agent_session_manager", None),
        "_agent_messages": [],
        "_context_controller": getattr(orch, "context_controller", None),
        "last_compact_at": None,
        "last_compact_turn": 0,
        "context_degradation_detected": False,
        "plan_attempts": 0,
        "replan_attempts": 0,
        "total_recovery_attempts": 0,
        "plan_enforce_warnings": False,
        "turn_count": 0,
        "max_turns": _compute_default_max_turns(orch),
        "plan_strict_mode": False,
        "call_graph": None,
        "test_map": None,
        "recent_tool_calls": [],
        "plan_dag": None,
        "execution_waves": None,
        "current_wave": 0,
        "preview_mode_enabled": False,
        "pending_preview_id": None,
        "awaiting_user_input": False,
        "preview_confirmed": None,
        "_should_distill": None,
        "_force_compact": None,
        "_budget_compaction": None,
        "_p2p_context": None,
        "action_failed": None,
        "delegation_depth": 0,
        "evaluation_result": None,
        "last_debug_error_type": None,
        "last_tool_name": None,
        "no_plan_fail_count": 0,
        "original_task": prompt,
        "plan_progress": None,
        "plan_validation": None,
        "planned_action": None,
        "replan_required": None,
        "step_description": None,
        "step_retry_counts": {},
        "task_history": None,
        "snapshots": [],
        "agent_mode": None,
        "parent_session_id": None,
        "_pending_injections_source": getattr(orch, "_injection_source", None),
    }


def prepare_system_prompt(
    *,
    orch: Any,
    system_prompt_name: Optional[str],
    load_system_prompt: Callable[[Optional[str]], Optional[str]],
) -> str:
    """Build the full runtime system prompt and sync it to the MessageManager."""
    full_system_prompt = (
        load_system_prompt(system_prompt_name)
        or "You are a helpful coding assistant."
    )

    try:
        from src.core.orchestration.instruction_loader import build_runtime_context

        runtime_ctx = build_runtime_context(cwd=orch.working_dir)
        if runtime_ctx:
            full_system_prompt = full_system_prompt + runtime_ctx
    except Exception:
        pass

    try:
        orch.msg_mgr.set_system_prompt(full_system_prompt)
    except Exception:
        pass

    return full_system_prompt


def resolve_max_turns(
    *,
    initial_state: Dict[str, Any],
    config_getter: Optional[Callable[[str], Any]] = None,
) -> int:
    """Resolve the effective max_turns with config override support."""
    max_turns = int(initial_state.get("max_turns") or 50)
    if config_getter is None:
        return max_turns
    try:
        cfg_max = config_getter("max_turns")
        if cfg_max is not None:
            max_turns = int(cfg_max)
    except Exception:
        pass
    return max_turns


def _compute_default_max_turns(orch: Any) -> int:
    """Return the tier-appropriate default max_turns for the active model."""
    try:
        from src.core.inference.model_tiers import classify_model, get_max_turns
        from src.core.inference.provider_utils import resolve_provider_and_model

        _, model = resolve_provider_and_model(orch)
        if not model:
            return 50
        adapter = getattr(orch, "_adapter", None) or getattr(orch, "adapter", None)
        ctx_window = int(getattr(adapter, "context_window", 0) or 0)
        tier = classify_model(model, ctx_window)
        return get_max_turns(tier)
    except Exception:
        return 50
