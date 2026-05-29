from langchain_core.runnables import RunnableConfig
import asyncio
import hashlib
import logging
from typing import Dict, Any, Optional, List

from src.core.orchestration.graph.state import StateLike, replace_state_list
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator, span_node as _span_node

logger = logging.getLogger(__name__)


async def replan_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _replan_node_impl."""
    with _span_node("replan", {"step": state.get("current_step", 0)}):
        return await _replan_node_impl(state, config)


async def _replan_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """
    Replan Node: Handles patch size violations by splitting oversized steps.
    When a patch exceeds 200 lines, this node prompts the LLM to rewrite
    the current step into 2-3 smaller, granular steps.
    Uses the 'strategic' role from ContextBuilder (loaded from agent-brain).
    """
    logger.info("=== replan_node START ===")

    replan_reason = state.get("replan_required", "Patch exceeded size limit")
    current_plan = state.get("current_plan") or []
    try:
        current_step = int(state.get("current_step") or 0)
    except (ValueError, TypeError):
        current_step = 0
    # C3: Clamp negative step — if the plan is empty there's nothing to replan
    if current_step < 0:
        current_step = 0
    if not current_plan:
        logger.warning("replan_node: current_plan is empty, nothing to replan")
        return {
            "replan_required": None,
            "action_failed": False,
            "replan_attempts": 0,
            "total_recovery_attempts": total_recovery_attempts,
            "errors": ["current_plan is empty"],
        }
    # P1-3: Increment inner replan-loop counter
    replan_attempts = int(state.get("replan_attempts") or 0) + 1
    # P2-A: global recovery cap (shared with debug_node)
    total_recovery_attempts = int(state.get("total_recovery_attempts") or 0) + 1

    # H-02: Detect LangGraph node replay by comparing the pre-replan plan hash.
    # On replay LangGraph re-reads the pre-node checkpoint, so replan_attempts
    # resets to the same value; the cap is never reached on a tight replay loop.
    # Guard: if the current plan hash equals last_plan_hash AND replan_required
    # is set, this is a replay of an already-attempted replan — abort early.
    try:
        import json as _json_h02
        _cur_hash = hashlib.sha256(
            _json_h02.dumps(current_plan, sort_keys=True, default=str).encode()
        ).hexdigest()
    except Exception:
        _cur_hash = None
    _prior_hash = state.get("last_plan_hash")
    if _cur_hash and _prior_hash and _cur_hash == _prior_hash:
        logger.warning(
            "replan_node: plan hash unchanged (H-02 replay guard) — skipping LLM replan"
        )
        return {
            "replan_required": None,
            "action_failed": False,
            "replan_attempts": replan_attempts,
            "total_recovery_attempts": total_recovery_attempts,
            "errors": ["replan_node: replay detected, plan hash unchanged — aborting"],
        }

    # Configurable replan ceiling (per-step)
    from src.core.config_loader import (
        get_agent_loop_constant,
        MAX_REPLAN_ATTEMPTS,
        MAX_TOTAL_RECOVERY_ATTEMPTS,
    )
    _replan_cap = get_agent_loop_constant("max_replan_attempts", MAX_REPLAN_ATTEMPTS)
    if replan_attempts >= _replan_cap:
        logger.warning(
            f"replan_node: replan_attempts={replan_attempts} exceeds cap={_replan_cap}, aborting"
        )
        return {
            "replan_required": None,
            "action_failed": False,
            "replan_attempts": replan_attempts,
            "total_recovery_attempts": total_recovery_attempts,
            "errors": [f"Replan cap ({_replan_cap}) exceeded — aborting replan loop"],
        }

    # P2-4: Global session-wide recovery ceiling (replan + debug combined).
    _total_recovery_cap = get_agent_loop_constant(
        "max_total_recovery_attempts", MAX_TOTAL_RECOVERY_ATTEMPTS
    )
    if total_recovery_attempts >= _total_recovery_cap:
        logger.warning(
            f"replan_node: total_recovery_attempts={total_recovery_attempts} exceeds "
            f"session cap={_total_recovery_cap}, aborting to prevent infinite loop"
        )
        return {
            "replan_required": None,
            "action_failed": False,
            "replan_attempts": replan_attempts,
            "total_recovery_attempts": total_recovery_attempts,
            "errors": [
                f"Session recovery cap ({_total_recovery_cap}) exceeded — "
                "aborting replan loop. Too many recovery attempts this session."
            ],
        }

    orchestrator = _resolve_orchestrator(state, config)
    if orchestrator is None:
        logger.error("replan_node: orchestrator is None")
        return {
            "replan_required": None,
            "action_failed": False,
            "replan_attempts": replan_attempts,
            "total_recovery_attempts": total_recovery_attempts,
            "errors": ["orchestrator not found"],
        }

    # Get the failed step description
    failed_step_desc = ""
    if current_plan and current_step < len(current_plan):
        failed_step_desc = current_plan[current_step].get("description", "Unknown step")

    logger.info(f"replan_node: replanning step - {failed_step_desc}")

    # Build prompt for splitting the step
    try:
        builder = ContextBuilder(working_dir=state.get("working_dir"))
        tools_list = [
            {"name": n, "description": m.get("description", "")}
            for n, m in orchestrator.tool_registry.tools.items()
        ]

        replan_prompt = f"""The previous step failed because the patch was too large:

Reason: {replan_reason}
Failed Step: {failed_step_desc}

Original Task: {state.get("original_task") or state.get("task")}

Please split this step into 2-3 smaller, targeted steps that can be executed independently.
Each step should be focused on a specific, manageable modification.

Return a JSON array of steps with this format:
[
  {{"description": "Step 1 description", "completed": false}},
  {{"description": "Step 2 description", "completed": false}},
  {{"description": "Step 3 description (if needed)", "completed": false}}
]

Respond ONLY with the JSON array, no other text."""

        # Conservative provider/model resolution (canonical pattern):
        # 1) orchestrator.get_provider_capabilities()
        # 2) ProviderManager.get_provider_capabilities(adapter)
        # 3) adapter attributes (provider, default_model, models)
        # Guard imports locally; only accept concrete strings (no MagicMock placeholders).
        from src.core.orchestration.provider_capabilities import resolve_provider_capabilities as _resolve_pc
        provider_capabilities = _resolve_pc(orchestrator)

        messages = builder.build_prompt(
            role_name="strategic",
            active_skills=[],
            task_description=replan_prompt,
            tools=tools_list,
            conversation=state.get("history", []),
            max_tokens=2000,
            provider_capabilities=provider_capabilities,
            model_tier=state.get("model_tier"),  # S1-B
            model_name=provider_capabilities.get("model") or "",
        )

        # WF-VOL21-1: Wrap LLM call with deadline guard (same pattern as planning_node).
        _replan_llm_timeout: int | None = 120
        try:
            from src.core.orchestration.project_settings import (
                get_active_settings as _gas_rp,
            )

            _ps_rp = _gas_rp()
            if _ps_rp is not None:
                _replan_llm_timeout = _ps_rp.max_llm_wait_seconds or None
        except Exception:
            pass

        cancel_event = state.get("cancel_event")
        if not cancel_event:
            cancel_event = getattr(orchestrator, "cancel_event", None)

        _rp_task = asyncio.create_task(
            call_model(
                messages,
                provider=provider_capabilities.get("provider") or None,
                model=provider_capabilities.get("model") or None,
                stream=False,
                format_json=False,
                # BUG-N3: pass session_id so token usage is attributed correctly
                session_id=state.get("session_id"),
            )
        )
        _rp_deadline = (
            asyncio.get_running_loop().time() + _replan_llm_timeout
            if _replan_llm_timeout
            else None
        )
        while not _rp_task.done():
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                _rp_task.cancel()
                logger.info("replan_node: Task canceled mid-generation")
                return {
                    "replan_required": None,
                    "action_failed": False,
                    "replan_attempts": replan_attempts,
                    "total_recovery_attempts": total_recovery_attempts,
                    "errors": ["canceled"],
                }
            if (
                _rp_deadline is not None
                and asyncio.get_running_loop().time() >= _rp_deadline
            ):
                _rp_task.cancel()
                logger.warning(
                    f"replan_node: LLM call timed out after {_replan_llm_timeout}s"
                )
                return {
                    "replan_required": None,
                    "action_failed": False,
                    "replan_attempts": replan_attempts,
                    "total_recovery_attempts": total_recovery_attempts,
                    "errors": [f"llm_timeout:{_replan_llm_timeout}s"],
                }
            await asyncio.wait([_rp_task], timeout=0.2)
        try:
            resp = await _rp_task
        except asyncio.CancelledError:
            raise

        content = ""
        if isinstance(resp, dict):
            _choices = resp.get("choices")
            if _choices and len(_choices) > 0:
                ch = (
                    _choices[0].get("message")
                    if isinstance(_choices[0], dict)
                    else None
                )
                if isinstance(ch, dict):
                    content = ch.get("content") or ""
            elif resp.get("message"):
                _msg = resp.get("message")
                if isinstance(_msg, dict):
                    content = _msg.get("content", "")

        # Parse the response to extract new steps
        import json
        import re

        new_steps = []
        try:
            # Try to extract JSON array from response
            json_match = re.search(r"\[[\s\S]*\]", content)
            if json_match:
                parsed = json.loads(json_match.group())
                # A3: Validate that the LLM returned a list of dicts with at
                # least a "description" or "action" key.  Reject malformed
                # payloads rather than letting non-dict items corrupt the plan
                # or cause a TypeError in dict(s) below.
                if isinstance(parsed, list):
                    sanitised = []
                    for item in parsed:
                        if isinstance(item, dict):
                            # C3: Ensure each step has a description or action
                            # key — missing either would break downstream
                            # nodes that expect one of these fields.
                            if "description" in item or "action" in item:
                                sanitised.append(item)
                            else:
                                logger.warning(
                                    "replan_node: skipping LLM step with neither "
                                    "description nor action: %r",
                                    item,
                                )
                        else:
                            logger.warning(
                                "replan_node: skipping non-dict step in LLM response: %r",
                                item,
                            )
                    new_steps = sanitised
                else:
                    logger.warning(
                        "replan_node: LLM response JSON is not a list (got %s), ignoring",
                        type(parsed).__name__,
                    )
                if new_steps:
                    logger.info("replan_node: generated %d new steps", len(new_steps))
        except Exception as e:
            logger.warning(f"replan_node: failed to parse LLM response: {e}")

        if new_steps and len(new_steps) > 0:
            # Replace the failed step with the new smaller steps
            if current_plan and current_step < len(current_plan):
                # Deep-copy existing step dicts to avoid sharing references with
                # the original state list (LangGraph immutability requirement, NEW-17)
                new_plan = (
                    [dict(s) for s in current_plan[:current_step]]
                    + new_steps
                    + [dict(s) for s in current_plan[current_step + 1 :]]
                )
                next_step = current_step  # Start from the first new step
            else:
                # C3: current_step is out of bounds — fall back to entire plan
                new_plan = new_steps
                next_step = 0

            # WR-6 fix: recompute execution_waves since step indices changed
            new_waves: Optional[List[List[str]]] = None
            try:
                from src.core.orchestration.dag_parser import _convert_flat_to_dag

                dag = _convert_flat_to_dag(new_plan)
                wave_ids = dag.topological_sort_waves()
                if wave_ids:
                    new_waves = wave_ids
                    logger.info(
                        f"replan_node: recomputed execution_waves: {len(wave_ids)} waves"
                    )
            except Exception as _e:
                logger.warning(
                    f"replan_node: failed to recompute execution_waves: {_e}"
                )

            logger.info(
                f"replan_node: replaced step {current_step + 1} with {len(new_steps)} new steps"
            )

            # WF-4: Hash the new plan so route_execution can detect identical replans
            import json as _json

            try:
                _plan_str = _json.dumps(new_plan, sort_keys=True, default=str)
                _new_hash = hashlib.sha256(_plan_str.encode()).hexdigest()
            except Exception:
                _new_hash = None

            return {
                "current_plan": new_plan,
                "current_step": next_step,
                "execution_waves": new_waves,  # WR-6 fix: include recomputed waves
                "replan_required": None,
                "action_failed": False,
                "replan_attempts": replan_attempts,
                "total_recovery_attempts": total_recovery_attempts,
                "last_plan_hash": _new_hash,
                # HR-13 fix: use system role with [internal] prefix to prevent
                # LLM from interpreting this as a user instruction.
                # H-05: wrap with replace_state_list so LangGraph replaces
                # rather than appends to the existing history list.
                "history": replace_state_list([
                    {
                        "role": "system",
                        "content": f"[internal] Replan: Split '{failed_step_desc}' into {len(new_steps)} smaller steps.",
                    }
                ]),
            }
        else:
            # Failed to generate new steps
            logger.warning("replan_node: no new steps generated, returning error")
            return {
                "replan_required": None,
                "action_failed": False,
                "replan_attempts": replan_attempts,
                "total_recovery_attempts": total_recovery_attempts,
                "errors": ["Failed to generate smaller steps"],
                # HR-13 fix: use system role with [internal] prefix.
                # H-05: wrap with replace_state_list so LangGraph replaces
                # rather than appends to the existing history list.
                "history": replace_state_list([
                    {
                        "role": "system",
                        "content": "[internal] Replan failed: Could not generate smaller steps.",
                    }
                ]),
            }

    except Exception as e:
        logger.error(f"replan_node: failed to replan: {e}")
        return {
            "replan_required": None,
            "action_failed": False,
            "replan_attempts": replan_attempts,
            "total_recovery_attempts": total_recovery_attempts,
            "errors": [f"replan failed: {e}"],
        }
