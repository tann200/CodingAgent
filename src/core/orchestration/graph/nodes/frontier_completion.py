"""Loop-completion bookkeeping for the frontier loop (P3.3).

Owns what the frontier loop says about *finishing*:

* :func:`build_exit_result` assembles the node's exit result.
* :func:`plan_completion_patch` records that the run covered its plan.
* :func:`persist_plan_progress` writes that record to the durable plan files.

The frontier loop runs a whole plan as one LLM+tool loop, so — unlike the
legacy ``execution_node`` — it never advances ``current_step`` and never sets a
per-step ``completed`` flag.  That left ``evaluation_node``'s ``plan_completed``
check unsatisfiable on the production tier graphs: ``current_step`` stayed 0 and
no step was ever flagged, so a fully successful run with an N-step plan was
judged "PARTIAL COMPLETION" and reported ``evaluation_result="replan"``.

The loop does have an authoritative completion signal — ``_completion_detected``,
set when the model answers a turn with no tool calls, i.e. it declares the work
finished.  That is the only claim made here: *the loop covered the plan and the
model said it was done*.  It is deliberately not a per-step claim, because the
loop has no per-step checkpoint and inventing one would put a falsehood in the
persisted plan.

Persisting the flag is what makes the memory useful: ``last_plan.json`` then
records that the plan is finished, so the next turn re-plans instead of silently
re-running completed work.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# Errors that mean "this run did not finish the plan" even when the model
# stopped asking for tools.  ``context_overflow`` and the P3.1 inline-verification
# cap both break the loop after work was abandoned mid-plan.
_INCOMPLETE_ERRORS = frozenset(
    {"context_overflow", "inline_verification_failed", "canceled"}
)


def _completion_declared(result: Optional[Mapping[str, Any]]) -> bool:
    """Return True when the loop's last result declares the work finished.

    ``frontier_loop_node`` sets the flag on ``last_result`` when a turn comes
    back with no tool calls, so it is read from there rather than from the exit
    result's top level.
    """
    if not isinstance(result, Mapping):
        return False
    last_result = result.get("last_result")
    if isinstance(last_result, Mapping):
        return last_result.get("_completion_detected") is True
    return result.get("_completion_detected") is True


def plan_completion_patch(
    state: Mapping[str, Any],
    result: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return the state patch recording that the loop completed its plan.

    Empty when there is no plan, when the loop did not declare completion, or
    when a blocking error / replan / approval suspension means the plan is not
    actually finished.  Requires ``_completion_detected`` rather than inferring
    completion from the absence of errors: a cut-short run has no errors either.
    """
    plan = state.get("current_plan")
    if not isinstance(plan, list) or not plan:
        return {}

    if not _completion_declared(result):
        return {}

    errors = state.get("errors") or []
    if isinstance(errors, list) and any(
        str(err) in _INCOMPLETE_ERRORS for err in errors
    ):
        return {}

    # A suspended approval or a pending replan means the plan is deferred, not
    # done.  Both are recorded on the result dict by frontier_loop_node.
    if isinstance(result, Mapping) and (
        result.get("replan_required") or result.get("awaiting_plan_approval")
    ):
        return {}

    logger.info(
        "frontier_completion: plan of %d step(s) completed by the loop", len(plan)
    )
    return {"plan_completed": True}


def build_exit_result(
    *,
    history: Any,
    tool_call_count: int,
    last_result: Optional[Dict[str, Any]],
    last_tool_name: Optional[str],
    errors: Any,
    turns_taken: int,
    inline_verifications: Any,
    replan_required: Optional[str],
    modified_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Assemble the frontier loop's exit result.

    Extracted verbatim from ``frontier_loop_node`` (P3.3) to keep that node
    inside its size guard.  ``last_result`` is replaced on context overflow
    unless it already records a failure, so a caller never sees "ok" for a run
    that was actually cut off by the context window.
    """
    result: Dict[str, Any] = {
        "history": history,
        "tool_call_count": tool_call_count,
        "last_result": last_result,
        "last_tool_name": last_tool_name,
        "errors": errors,
        "_frontier_loop_turns": turns_taken,
        "awaiting_plan_approval": False,
        "modified_files": modified_files or [],
    }
    if inline_verifications:
        failures = [r for r in inline_verifications if r.get("status") == "failed"]
        if failures:
            result["inline_verification_failures"] = failures
    if replan_required:
        result["replan_required"] = replan_required
        result["action_failed"] = True
        result["next_action"] = None
    if "context_overflow" in (errors or []):
        result["_budget_compaction"] = True
        result["_should_distill"] = True
        if last_result is None or last_result.get("ok") is not False:
            result["last_result"] = {
                "ok": False,
                "error": "Context window overflow — compaction triggered",
            }
    return result


def _plan_context_path_fn() -> Any:
    """Return ``agent_context_path``, or None when tools_config is unavailable."""
    try:
        from src.tools.tools_config import agent_context_path

        return agent_context_path
    except Exception:
        return None


def persist_plan_progress(
    orchestrator: Any,
    state: Mapping[str, Any],
) -> bool:
    """Persist plan completion to the durable plan records.

    Writes ``last_plan.json`` (via the shared ``save_last_plan`` helper, so there
    is still exactly one writer for that file) and appends to the append-only
    ``plan_history.jsonl``.  Never raises: losing the progress record must not
    fail the run that already did the work.
    """
    plan = state.get("current_plan")
    if not isinstance(plan, list) or not plan:
        return False

    # Fail closed on a missing working_dir — defaulting to "." would write this
    # run's plan into whatever directory the process happens to be in.
    workdir = getattr(orchestrator, "working_dir", None)
    if not workdir:
        logger.warning(
            "frontier_completion: no working_dir — plan progress not persisted"
        )
        return False

    task = str(state.get("task") or "")
    # current_step is left at the plan length: the plan ran to its end, so the
    # progress index is past-the-end rather than a per-step claim.
    step = len(plan)
    ctx_path_fn = _plan_context_path_fn()

    try:
        from src.core.orchestration.graph.nodes.planning_helpers import (
            append_plan_audit_log,
            get_last_plan_path,
            save_last_plan,
        )

        def _plan_path(workdir_arg: str) -> Path:
            return get_last_plan_path(
                workdir=workdir_arg, agent_context_path_fn=ctx_path_fn
            )

        _kw: Dict[str, Any] = dict(
            workdir=str(workdir),
            plan=plan,
            task=task,
            step=step,
            get_last_plan_path_fn=_plan_path,
            logger=logger,
        )
        # ``extra`` carries the completion flag; the canonical identity keys in
        # save_last_plan always win over it.  save_last_plan reports whether the
        # record landed — a silently-lost progress record would leave the next
        # turn re-running finished work, which is the bug P3.3 closes.
        saved = save_last_plan(extra={"plan_completed": True}, **_kw)  # type: ignore[arg-type]
        append_plan_audit_log(reason="plan_completed", **_kw)  # type: ignore[arg-type]
    except Exception as exc:
        logger.warning("frontier_completion: persist failed (non-fatal): %s", exc)
        return False

    if not saved:
        logger.warning(
            "frontier_completion: completion flag did not reach %s — the next turn "
            "may re-plan completed work",
            workdir,
        )
        return False

    logger.info(
        "frontier_completion: persisted completion of %d-step plan to %s",
        len(plan),
        workdir,
    )
    return True


def handle_no_tool_completion(
    *,
    task: str,
    content_text: Optional[str],
    turns_taken: int,
    tool_call_count: int,
    state: Mapping[str, Any],
    orchestrator: Any,
    last_result: Optional[Mapping[str, Any]] = None,
    last_tool_name: Optional[str] = None,
    mutating_tool_calls_count: int = 0,
    errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Evaluate completion contracts when the model returns no tool calls."""
    from src.core.orchestration.graph.completion_contract import evaluate_completion_contract
    from src.core.security.redaction import redact_secrets

    modified_files: set[str] = set()
    if orchestrator and hasattr(orchestrator, "_session_modified_files") and orchestrator._session_modified_files:
        modified_files.update(orchestrator._session_modified_files)
    if state.get("modified_files"):
        modified_files.update(state.get("modified_files"))
    if state.get("_session_modified_files"):
        modified_files.update(state.get("_session_modified_files"))

    effective_last_result = last_result if last_result is not None else state.get("last_result")
    effective_errors = errors if errors is not None else state.get("errors")

    contract = evaluate_completion_contract(
        task=task,
        content_text=content_text,
        tool_calls_count=tool_call_count,
        modified_files=modified_files,
        current_plan=state.get("current_plan"),
        affected_files=state.get("affected_files"),
        last_result=effective_last_result,
        errors=effective_errors,
        last_tool_name=last_tool_name or state.get("last_tool_name"),
        mutating_tool_calls_count=mutating_tool_calls_count,
    )

    if contract.is_complete:
        content_preview = redact_secrets((content_text or "")[:200])
        logger.info(
            "frontier_loop_node: no tool calls in turn %d — task complete (type=%s). "
            "Content preview: %r",
            turns_taken,
            contract.task_type.value,
            content_preview,
        )
        return {
            "ok": True,
            "completed_without_tool": True,
            "_completion_detected": True,
            "task_type": contract.task_type.value,
        }

    logger.warning(
        "frontier_loop_node: completion rejected by contract in turn %d: %s (type=%s)",
        turns_taken,
        contract.failure_reason,
        contract.task_type.value,
    )
    return {
        "ok": False,
        "error": contract.failure_reason or "Completion contract unsatisfied",
        "error_code": contract.error_code or "completion_contract_failed",
        "task_type": contract.task_type.value,
        "_completion_detected": False,
    }

