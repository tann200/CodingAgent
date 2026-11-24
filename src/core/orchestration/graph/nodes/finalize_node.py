"""Finalize Node for the unified 6-node 2026 LangGraph cognitive pipeline.

Completes agent run:
- Enforces the fail-closed Transaction Settlement Guard on open step snapshots
  so unverified, failed, or cancelled deferred-rollback edits never leak to disk.
- Performs on-demand memory compaction / distillation (inlined from legacy
  ``memory_update_node.py`` in TASK-25 Stage A) without blocking short tasks.
- Clears transient pipeline errors while preserving persistent errors.
- Guarantees terminal exit to END.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from langchain_core.runnables import RunnableConfig

from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    span_node as _span_node,
)
from src.core.orchestration.graph.state import StateLike, replace_state_list

try:
    from src.core.memory.distiller import distill_context
except ImportError as _distiller_err:
    distill_context = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "finalize_node: distiller unavailable (%s) — context compaction disabled",
        _distiller_err,
    )

logger = logging.getLogger(__name__)

_TRANSIENT_ERROR_PREFIXES = (
    "context_overflow",
    "tool_timeout",
    "kv_cache",
    "token_limit",
)


def _finalize_memory_sync(state: StateLike) -> Dict[str, Any]:
    """Run on-demand compaction / optional summary sync and strip transient errors.

    Inlined from legacy ``memory_update_node.py`` (TASK-25 Stage A).
    """
    working_dir = state.get("working_dir", "unknown")
    workdir_path = Path(working_dir)
    history = state.get("history", [])
    session_id = state.get("session_id")

    _budget_forced_compact = False
    try:
        from src.core.orchestration.token_budget import get_token_budget_monitor

        _monitor = get_token_budget_monitor()
        if _monitor.check_budget(state) == "compact":
            _budget_forced_compact = True
            _monitor.check_and_prepare_compaction(session_id or "default")
            logger.info("finalize_node: token budget threshold reached — forcing compact")
    except Exception as _budget_err:
        logger.debug("finalize_node: token budget check skipped: %s", _budget_err)

    should_distill = state.get("_should_distill", False)
    force_compact = bool(state.get("_force_compact", False)) or _budget_forced_compact

    _updated_history = None
    _distilled_summary: str = ""

    if should_distill or force_compact:
        try:
            from src.core.memory.compaction_service import CompactionService

            _event_bus = None
            try:
                from src.core.orchestration.event_bus import get_event_bus

                _event_bus = get_event_bus()
            except Exception:
                pass

            svc = CompactionService(
                history=list(history),
                working_dir=workdir_path,
                event_bus=_event_bus,
                prefer_deterministic=False,
            )
            if force_compact or svc.should_compact():
                res = svc.compact()
                if res.success and res.compacted_history:
                    _updated_history = res.compacted_history
                    logger.info(
                        "finalize_node: history compacted (method=%s, %d->%d messages)",
                        res.method,
                        len(history),
                        len(_updated_history),
                    )
                elif force_compact:
                    _updated_history = [
                        {"role": "system", "content": "Session Summary:\nContext compacted."},
                        {"role": "user", "content": state.get("task", "")},
                    ]

            if should_distill and distill_context is not None:
                try:
                    distilled = distill_context(list(history), working_dir=workdir_path)
                    if isinstance(distilled, dict):
                        if not _updated_history and distilled.get("_compacted_history"):
                            _updated_history = distilled["_compacted_history"]
                        if distilled.get("current_state"):
                            _distilled_summary = str(distilled["current_state"])
                except Exception as _me3_err:
                    logger.debug("finalize_node: distill summary skipped: %s", _me3_err)
        except Exception as exc:
            logger.error("finalize_node: memory sync failed: %s", exc)

    prior_errors: list = list(state.get("errors") or [])  # type: ignore[arg-type]
    persistent_errors = [
        e
        for e in prior_errors
        if not any(str(e).startswith(pfx) for pfx in _TRANSIENT_ERROR_PREFIXES)
    ]

    out: Dict[str, Any] = {"_force_compact": False, "errors": persistent_errors}
    if _distilled_summary:
        out["analysis_summary"] = _distilled_summary
    if _updated_history is not None:
        out["history"] = replace_state_list(_updated_history)
    return out


async def finalize_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _finalize_node_impl."""
    with _span_node("finalize", {"turn": state.get("turn_count", 0)}):
        return await _finalize_node_impl(state, config)


async def _finalize_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Terminal execution node with fail-closed Transaction Settlement Guard."""
    logger.info("=== finalize_node START ===")
    run_state = dict(state.get("run") or {})
    orchestrator = _resolve_orchestrator(state, config)

    cancel_event = state.get("cancel_event") or (
        getattr(orchestrator, "cancel_event", None) if orchestrator else None
    )
    if (
        cancel_event is not None
        and hasattr(cancel_event, "is_set")
        and callable(cancel_event.is_set)
        and cancel_event.is_set() is True
    ):
        run_state["status"] = "cancelled"
        run_state["outcome"] = "cancelled"

    outcome = run_state.get("outcome") or "complete"
    if outcome in ("complete", "verify", "continue"):
        if run_state.get("status") not in ("failed", "cancelled"):
            run_state["status"] = "completed"
            run_state["outcome"] = "complete"

    # Transaction Settlement Guard: settle any open deferred-rollback snapshot
    snap_id = getattr(orchestrator, "_step_snapshot_id", None) if orchestrator else None
    if isinstance(snap_id, str) and snap_id:
        is_cancelled_or_failed = (
            run_state.get("status") in ("failed", "cancelled")
            or run_state.get("outcome") in ("failed", "cancelled")
            or (
                cancel_event is not None
                and hasattr(cancel_event, "is_set")
                and callable(cancel_event.is_set)
                and cancel_event.is_set() is True
            )
        )
        can_commit = (not is_cancelled_or_failed) and (
            state.get("verification_passed") is True
            or state.get("use_verification") is False
        )
        if can_commit:
            try:
                if hasattr(orchestrator, "commit_step_transaction"):
                    orchestrator.commit_step_transaction()
                    logger.info("finalize_node: committed open snapshot %s", snap_id)
            except Exception as exc:
                logger.warning("finalize_node: commit_step_transaction error: %s", exc)
        else:
            # Fail closed: never leave unverified/failed/cancelled mutations on disk
            try:
                if hasattr(orchestrator, "rollback_step_transaction"):
                    rb = orchestrator.rollback_step_transaction()
                    logger.info(
                        "finalize_node: settlement guard rolled back unverified snapshot %s (restored=%s)",
                        snap_id,
                        rb.get("restored_count", 0) if isinstance(rb, dict) else "unknown",
                    )
            except Exception as exc:
                logger.warning("finalize_node: rollback_step_transaction error: %s", exc)
            if run_state.get("status") != "cancelled":
                run_state["status"] = "failed"
                run_state["outcome"] = "failed"

    eval_verdict = state.get("evaluation_result") or (
        "complete" if run_state.get("status") == "completed" else "failed"
    )
    mem_state = dict(state)
    mem_state["evaluation_result"] = eval_verdict
    if "_should_distill" not in state and not state.get("_force_compact"):
        mem_state["_should_distill"] = False

    res = _finalize_memory_sync(mem_state)
    res["run"] = run_state
    if not res.get("evaluation_result"):
        res["evaluation_result"] = eval_verdict

    logger.info(
        "finalize_node: pipeline completed with status=%s, outcome=%s",
        run_state.get("status"),
        run_state.get("outcome"),
    )
    return res
