"""Unified Recovery Node for the 7-node LangGraph cognitive pipeline.

Replaces the legacy replan/debug split per Engineering Audit §Appendix:
- Receives failed action, error classification, verification evidence, and action signature.
- Rejects duplicate failed action signatures.
- Monotonically tracks recovery budget from immutable tier policy.
- Produces one replacement action or routes to finalize when unrecoverable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from langchain_core.runnables import RunnableConfig

from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.graph.nodes.debug_node import _classify_error
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    span_node as _span_node,
)
from src.core.orchestration.graph.policy import (
    compute_action_digest,
)
from src.core.orchestration.graph.state import StateLike
from src.core.orchestration.tool_parser import parse_tool_block

logger = logging.getLogger(__name__)


async def recover_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _recover_node_impl."""
    with _span_node("recover", {"step": state.get("current_step", 0)}):
        return await _recover_node_impl(state, config)


async def _recover_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Unified recovery node implementation."""
    logger.info("=== recover_node START ===")
    orchestrator = _resolve_orchestrator(state, config)

    # 1. Inspect run state & recovery budget
    run_state = dict(state.get("run") or {})
    runtime_state = dict(state.get("runtime") or {})
    policy = dict(runtime_state.get("policy") or {})
    recovery_limit = int(policy.get("recovery_limit") or 2)
    recoveries_taken = int(run_state.get("recoveries") or state.get("debug_attempts") or 0)

    # 2. Extract failed action and evidence
    last_action = state.get("last_action")
    failed_tool_name = ""
    failed_args: Dict[str, Any] = {}
    failed_digest = ""
    if isinstance(last_action, Mapping):
        failed_tool_name = str(last_action.get("name") or "")
        failed_args = dict(last_action.get("arguments") or {})
        failed_digest = str(last_action.get("digest") or "")
    if not failed_tool_name:
        failed_tool_name = str(state.get("last_tool_name") or "")
    if not failed_digest and failed_tool_name:
        failed_digest = compute_action_digest(failed_tool_name, failed_args)

    # Extract error message / verification evidence
    last_result = state.get("last_result") or {}
    verification = state.get("verification") or {}
    verification_result = state.get("verification_result") or {}

    error_summary = ""
    if isinstance(last_result, Mapping):
        err = last_result.get("error") or last_result.get("result", {}).get("error") if isinstance(last_result.get("result"), Mapping) else None
        if err:
            error_summary = str(err)
    if not error_summary and isinstance(verification, Mapping):
        evidence = verification.get("evidence") or []
        if evidence:
            error_summary = "; ".join(str(e) for e in evidence[:3])
    if not error_summary and isinstance(verification_result, Mapping):
        for k, v in verification_result.items():
            if isinstance(v, Mapping) and (v.get("status") in ("fail", "failed") or v.get("passed") is False):
                error_summary += f"{k}: {v.get('stdout') or v.get('error') or ''} "
    if not error_summary:
        errors = state.get("errors") or []
        if errors:
            error_summary = "; ".join(str(e) for e in errors[-3:])
    if not error_summary:
        error_summary = "Action failed without detailed diagnostic output"

    error_type = _classify_error(error_summary)
    logger.info("recover_node: classified error %r (attempt %d/%d)", error_type, recoveries_taken, recovery_limit)

    # 3. Check if recovery budget is exhausted
    if recoveries_taken >= recovery_limit:
        logger.warning("recover_node: recovery limit (%d) reached — unrecoverable", recovery_limit)
        try:
            rollback_mgr = getattr(orchestrator, "rollback_manager", None)
            if rollback_mgr and rollback_mgr.current_snapshot:
                rollback_mgr.rollback()
        except Exception as rb_err:
            logger.warning("recover_node: auto-rollback failed: %s", rb_err)

        run_state["status"] = "failed"
        run_state["outcome"] = "failed"
        return {
            "run": run_state,
            "errors": [f"Recovery limit ({recovery_limit}) reached for {error_type}"],
            "action_failed": True,
            "next_action": None,
            "pending_action": None,
        }

    # 4. Prompt LLM for replacement action
    task = state.get("task", "")
    working_dir = state.get("working_dir", ".")
    model_tier = (state.get("model_tier") or runtime_state.get("tier") or "medium").lower()

    builder = ContextBuilder(orchestrator=orchestrator, working_dir=working_dir)
    messages = builder.build(
        task=task,
        history=state.get("history", []),
        role="reviewer",
        model_tier=model_tier,
    )

    recovery_prompt = (
        f"The previous action failed. Diagnose the root cause and provide a replacement tool action to resolve it.\n\n"
        f"Failed Action: {failed_tool_name}\n"
        f"Error Type: {error_type}\n"
        f"Error Details: {error_summary[:1000]}\n\n"
        "Output ONLY a single valid tool invocation JSON or tool call to replace the failed action. "
        "Do not repeat the identical failed parameters."
    )
    messages.append({"role": "user", "content": recovery_prompt})

    llm_kwargs: Dict[str, Any] = {}
    if getattr(orchestrator, "deterministic", False):
        llm_kwargs["temperature"] = 0.0
        if getattr(orchestrator, "seed", None) is not None:
            llm_kwargs["seed"] = orchestrator.seed
    else:
        llm_kwargs["temperature"] = 0.1

    cancel_event = state.get("cancel_event")
    loop = None
    try:
        import asyncio
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    try:
        response = await call_model(
            messages=messages,
            cancel_event=cancel_event,
            loop=loop,
            **llm_kwargs,
        )
    except Exception as exc:
        logger.error("recover_node: LLM call failed: %s", exc)
        run_state["status"] = "failed"
        run_state["outcome"] = "failed"
        return {
            "run": run_state,
            "errors": [f"LLM recovery generation failed: {exc}"],
            "action_failed": True,
            "next_action": None,
            "pending_action": None,
        }

    content_text = response.content if hasattr(response, "content") else str(response)
    candidate_action = parse_tool_block(content_text)

    # 5. Invariant: Reject duplicate failed signatures
    if candidate_action and isinstance(candidate_action, Mapping):
        cand_name = str(candidate_action.get("name") or "")
        cand_args = dict(candidate_action.get("arguments") or {})
        cand_digest = compute_action_digest(cand_name, cand_args)
        if cand_digest == failed_digest:
            logger.warning("recover_node: rejected duplicate failed action signature %s", cand_digest)
            candidate_action = None

    new_recoveries = recoveries_taken + 1
    run_state["recoveries"] = new_recoveries

    if candidate_action:
        logger.info("recover_node: generated replacement action %r", candidate_action.get("name"))
        run_state["status"] = "running"
        run_state["outcome"] = "continue"

        digest = compute_action_digest(
            str(candidate_action.get("name") or ""),
            candidate_action.get("arguments") or {},
        )
        pending_act = {
            "name": str(candidate_action.get("name") or ""),
            "arguments": dict(candidate_action.get("arguments") or {}),
            "summary": f"Replacement action for failed {failed_tool_name}",
            "digest": digest,
        }

        feedback_msg = {
            "role": "user",
            "content": f"[Recovery] Previous {failed_tool_name} failed with {error_type}: {error_summary[:400]}. Proposing replacement action {candidate_action.get('name')}.",
        }

        return {
            "run": run_state,
            "pending_action": pending_act,
            "next_action": dict(candidate_action),
            "action_failed": False,
            "replan_required": None,
            "history": [feedback_msg],
            "debug_attempts": new_recoveries,
            "total_debug_attempts": int(state.get("total_debug_attempts") or 0) + 1,
        }

    logger.warning("recover_node: failed to produce a valid non-duplicate replacement action")
    run_state["status"] = "failed"
    run_state["outcome"] = "failed"
    return {
        "run": run_state,
        "action_failed": True,
        "next_action": None,
        "pending_action": None,
        "errors": [f"Unrecoverable {error_type}: no valid replacement action produced"],
        "debug_attempts": new_recoveries,
    }
