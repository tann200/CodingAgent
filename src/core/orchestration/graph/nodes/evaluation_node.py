import asyncio
import json
import logging
from typing import Mapping, Dict, Any

from src.core.orchestration.graph.state import AgentState

logger = logging.getLogger(__name__)


async def evaluation_node(state: Mapping[str, Any], config: Any) -> Dict[str, Any]:
    """
    Evaluation Node: Post-verification review to decide if task goal is fully met.
    Reviews overall state including verification results, plan completion, and errors.
    Routes to: end (task complete), memory_sync (save and end), or replan (retry).
    Uses the 'reviewer' role for quality assurance.
    """
    logger.info("=== evaluation_node START ===")

    verification_result = state.get("verification_result") or {}
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    errors = state.get("errors") or []

    # Prefer the authoritative boolean already computed by verification_node.
    # Fall back to recomputing only when the field is absent (e.g. the first run
    # before verification_node has ever executed).
    _state_vp = state.get("verification_passed")
    if _state_vp is not None:
        verification_passed = bool(_state_vp)
        failure_reasons: list = []

        # Still collect human-readable reasons for the log even when using the flag.
        def _failed(r: dict) -> bool:
            return isinstance(r, dict) and r.get("status") == "fail"

        # Python keys
        for key, label in (
            ("tests", "Tests"),
            ("linter", "Linter"),
            ("syntax", "Syntax"),
        ):
            r = verification_result.get(key, {})
            if _failed(r):
                failure_reasons.append(
                    f"{label} failed: {r.get('message', r.get('stdout', 'Unknown error'))[:200]}"
                )
        # JS/TS keys
        for key, label in (
            ("js_tests", "JS tests"),
            ("ts_check", "TypeScript check"),
            ("eslint", "ESLint"),
        ):
            r = verification_result.get(key, {})
            if _failed(r):
                failure_reasons.append(
                    f"{label} failed: {r.get('message', r.get('stdout', 'Unknown error'))[:200]}"
                )
    else:
        # Recompute from scratch — covers both Python and JS/TS result keys.
        verification_passed = True
        failure_reasons = []

        def _failed(r: dict) -> bool:  # type: ignore[misc]
            return isinstance(r, dict) and r.get("status") == "fail"

        # Python verification keys
        for key, label in (
            ("tests", "Tests"),
            ("linter", "Linter"),
            ("syntax", "Syntax"),
        ):
            r = verification_result.get(key, {})
            if _failed(r):
                verification_passed = False
                failure_reasons.append(
                    f"{label} failed: {r.get('message', r.get('stdout', 'Unknown error'))[:200]}"
                )

        # JS/TS verification keys (CF-2 fix: previously invisible to evaluation)
        for key, label in (
            ("js_tests", "JS tests"),
            ("ts_check", "TypeScript check"),
            ("eslint", "ESLint"),
        ):
            r = verification_result.get(key, {})
            if _failed(r):
                verification_passed = False
                failure_reasons.append(
                    f"{label} failed: {r.get('message', r.get('stdout', 'Unknown error'))[:200]}"
                )

    # Check plan completion
    plan_completed = (
        not current_plan
        or current_step >= len(current_plan)
        or all(step.get("completed", False) for step in current_plan)
    )

    # Check for critical errors
    critical_errors = [
        e for e in errors if e not in ["canceled", "orchestrator not found"]
    ]

    # Read debug_attempts to gate debug routing — this prevents an unbounded loop.
    # Previously evaluation routed to "replan" on verification failure, which sent to
    # step_controller → execution → verification → evaluation (rounds never incremented
    # in this path, so the rounds < 10 guard never fired → infinite loop).
    # Now: route to "debug" for verification failures (bounded by debug_attempts).
    debug_attempts = int(state.get("debug_attempts") or 0)
    max_debug_attempts = int(state.get("max_debug_attempts") or 3)

    # Determine outcome
    if verification_passed and plan_completed and not critical_errors:
        logger.info("evaluation_node: TASK COMPLETE - all checks passed")
        # MC-3 fix: record the task decision in the session store so historical
        # reasoning traces are available for debugging and cross-task learning.
        try:
            from src.core.orchestration.graph.nodes.node_utils import (
                _resolve_orchestrator,
            )

            _orch = _resolve_orchestrator(state, config)
            if _orch and hasattr(_orch, "session_store") and _orch.session_store:
                _session_id = state.get("session_id") or "unknown"
                _task_desc = (state.get("task") or "")[:200]
                _orch.session_store.add_decision(
                    session_id=_session_id,
                    decision=f"complete: {_task_desc}",
                    rationale="All verification checks passed; plan fully executed.",
                )
        except Exception as _de:
            logger.debug(f"evaluation_node: add_decision failed (non-critical): {_de}")

        # WF-2: Semantic LLM verdict — only when rule-based check says "complete".
        # PERF-2: Skipped when enable_semantic_evaluation=False in project settings.
        # P1-A fix: default is now False — must be explicitly enabled.  Previously
        # the default was True, so any settings import failure left this permanently
        # enabled, firing an extra LLM call on every task completion.
        # Never blocks completion; wrapped in try/except so any failure is silently ignored.
        llm_verdict: str | None = None
        llm_reason: str | None = None
        _semantic_enabled = False
        try:
            from src.core.orchestration.project_settings import (
                get_active_settings as _gas,
            )

            _ps = _gas()
            if _ps is not None:
                _semantic_enabled = bool(_ps.enable_semantic_evaluation)
        except Exception:
            pass  # stays False on any failure

        if _semantic_enabled:
            try:
                from src.core.inference.llm_manager import call_model as _call_model

                _verdict_prompt = [
                    {
                        "role": "system",
                        "content": (
                            "You are a code review judge. Answer ONLY with PASS or FAIL "
                            "followed by one concise sentence explaining why."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            # DOOM-LOOP FIX: use original_task as authoritative task
                            # string; state["task"] may have been clobbered by planning_node.
                            f"Task: {state.get('original_task') or state.get('task', '')}\n\n"
                            f"Plan executed:\n{json.dumps(current_plan, indent=2)}\n\n"
                            f"Verification result: {json.dumps(verification_result)}\n\n"
                            "Did the agent accomplish the task? Reply PASS or FAIL."
                        ),
                    },
                ]
                # WF-VOL21-3: Wrap bare await with asyncio.wait_for to bound the
                # semantic evaluation call; default 120 s, respects project settings.
                _eval_timeout: int | None = 120
                try:
                    from src.core.orchestration.project_settings import (
                        get_active_settings as _gas_ev,
                    )

                    _ps_ev = _gas_ev()
                    if _ps_ev is not None:
                        _eval_timeout = _ps_ev.max_llm_wait_seconds or None
                except Exception:
                    pass

                _resp = await asyncio.wait_for(
                    _call_model(_verdict_prompt, model=None),
                    timeout=_eval_timeout,
                )
                _content = (
                    (_resp.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                _llm_passed = "FAIL" not in (_content or "").upper()
                llm_verdict = "pass" if _llm_passed else "fail"
                llm_reason = (_content or "")[:200]
                logger.info(
                    f"evaluation_node: WF-2 LLM verdict={llm_verdict!r} reason={llm_reason!r}"
                )
                if not _llm_passed:
                    # Downgrade: route to debug instead of complete
                    return {
                        "evaluation_result": "debug",
                        "evaluation_llm_verdict": llm_verdict,
                        "evaluation_llm_reason": llm_reason,
                    }
            except Exception as _wf2_err:
                logger.debug(
                    f"evaluation_node: WF-2 LLM verdict failed (non-critical): {_wf2_err}"
                )

        return {
            "evaluation_result": "complete",
            "next_action": None,
            "evaluation_llm_verdict": llm_verdict,
            "evaluation_llm_reason": llm_reason,
        }
    elif not verification_passed:
        logger.info(f"evaluation_node: VERIFICATION FAILED - {failure_reasons}")
        # Route to debug if we have debug attempts remaining — this is the bounded fix path.
        # debug_node generates a fix → execution → verification → evaluation cycle,
        # but is capped by debug_attempts so it cannot loop indefinitely.
        if debug_attempts < max_debug_attempts:
            logger.info(
                f"evaluation_node: routing to debug (attempt {debug_attempts + 1}/{max_debug_attempts})"
            )
            return {
                "evaluation_result": "debug",
            }
        else:
            logger.warning(
                f"evaluation_node: max debug attempts ({max_debug_attempts}) reached, ending"
            )
            return {
                "evaluation_result": "end",
                "next_action": None,
            }
    elif critical_errors:
        logger.info(f"evaluation_node: CRITICAL ERRORS - {critical_errors}")
        return {
            "evaluation_result": "end",
            "next_action": None,
            "errors": critical_errors,
        }
    else:
        # Partial completion — plan has remaining steps (shouldn't normally happen since
        # execution only reaches verification after the last step, but guard defensively).
        logger.info(
            "evaluation_node: PARTIAL COMPLETION - checking if more work needed"
        )
        if current_plan and current_step < len(current_plan):
            # There are pending steps: re-enter execution via step_controller.
            # This is bounded because plan steps are finite; as steps complete,
            # current_step advances until current_step >= len(current_plan).
            # W5 fix: do NOT set replan_required here — that field is consumed by
            # should_after_execution_with_replan and would route subsequent execution
            # steps to replan_node (splitting) instead of step_controller (advancing).
            # Clear it explicitly to prevent stale value from a prior F13 trigger.
            return {
                "evaluation_result": "replan",
                "replan_required": None,
                "action_failed": False,
            }
        else:
            return {
                "evaluation_result": "complete",
                "next_action": None,
            }
