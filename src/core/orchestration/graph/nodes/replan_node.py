import asyncio
import hashlib
import logging
from typing import Dict, Any, Optional, List

from src.core.orchestration.graph.state import StateLike
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator

logger = logging.getLogger(__name__)


async def replan_node(state: StateLike, config: Any) -> Dict[str, Any]:
    """
    Replan Node: Handles patch size violations by splitting oversized steps.
    When a patch exceeds 200 lines, this node prompts the LLM to rewrite
    the current step into 2-3 smaller, granular steps.
    Uses the 'strategic' role from ContextBuilder (loaded from agent-brain).
    """
    logger.info("=== replan_node START ===")

    replan_reason = state.get("replan_required", "Patch exceeded size limit")
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    # P1-3: Increment inner replan-loop counter
    replan_attempts = int(state.get("replan_attempts") or 0) + 1
    # P2-A: global recovery cap (shared with debug_node)
    total_recovery_attempts = int(state.get("total_recovery_attempts") or 0) + 1

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
        provider_capabilities = {}
        try:
            try:
                from src.core.utils.strings import (
                    valid_str as _valid_str,
                    extract_str as _extract_str,
                )
            except Exception:

                def _valid_str(x: object) -> bool:
                    return (
                        isinstance(x, str)
                        and bool(x.strip())
                        and ("MagicMock" not in x)
                    )

                def _extract_str(candidate: object) -> str | None:
                    if candidate is None:
                        return None
                    if isinstance(candidate, dict):
                        for key in (
                            "provider_name",
                            "name",
                            "id",
                            "key",
                            "model",
                            "default_model",
                            "type",
                        ):
                            val = candidate.get(key)
                            if isinstance(val, str) and _valid_str(val):
                                return val.strip()
                        return None
                    if isinstance(candidate, str) and _valid_str(candidate):
                        return candidate.strip()
                    return None

            caps: dict = {}

            # 1) Orchestrator-level capabilities
            try:
                if (
                    orchestrator
                    and hasattr(orchestrator, "get_provider_capabilities")
                    and callable(getattr(orchestrator, "get_provider_capabilities"))
                ):
                    _rc = orchestrator.get_provider_capabilities()
                    if isinstance(_rc, dict) and _rc:
                        caps = dict(_rc)
            except Exception:
                caps = {}

            # 2) ProviderManager fallback
            if not caps:
                try:
                    from src.core.inference.llm_manager import (
                        get_provider_manager as _gpm,
                    )

                    _pm = _gpm()
                    adapter = getattr(orchestrator, "_adapter", None)
                    _rc = _pm.get_provider_capabilities(adapter)
                    if isinstance(_rc, dict) and _rc:
                        caps = dict(_rc)
                except Exception:
                    caps = caps or {}

            # 3) Adapter-only fallback
            if not caps and orchestrator and getattr(orchestrator, "_adapter", None):
                adapter = orchestrator._adapter
                try:
                    prov_attr = getattr(adapter, "provider", None)
                except Exception:
                    prov_attr = None
                provider_name = None
                try:
                    provider_name = _extract_str(prov_attr)
                except Exception:
                    provider_name = None
                if not provider_name:
                    try:
                        provider_name = _extract_str(getattr(adapter, "name", None))
                    except Exception:
                        provider_name = None

                model = None
                try:
                    model = _extract_str(getattr(adapter, "default_model", None))
                except Exception:
                    model = None
                if not model:
                    try:
                        models_attr = getattr(adapter, "models", None)
                        if isinstance(models_attr, (list, tuple)):
                            for m in models_attr:
                                mm = _extract_str(m)
                                if mm:
                                    model = mm
                                    break
                        else:
                            model = _extract_str(models_attr)
                    except Exception:
                        model = None

                supports_native_tools = False
                try:
                    if isinstance(prov_attr, dict):
                        supports_native_tools = bool(
                            prov_attr.get("supports_native_tools", False)
                        )
                    else:
                        supports_native_tools = bool(
                            getattr(adapter, "supports_native_tools", False)
                        )
                except Exception:
                    supports_native_tools = False

                provider_family = "default"
                try:
                    from src.core.orchestration.provider_capabilities import (
                        _map_provider_family_impl as _map_pf,
                    )

                    provider_family = _map_pf(provider_name or "")
                except Exception:
                    provider_family = "default"

                caps = {
                    "supports_native_tools": bool(supports_native_tools),
                    "provider_family": provider_family,
                    "model": model,
                    "provider_name": provider_name or "",
                }

            # Sanitize and expose conservative provider_capabilities
            try:
                _pname = _extract_str(
                    caps.get("provider_name")
                    or caps.get("provider")
                    or caps.get("name")
                )
            except Exception:
                _pname = None
            try:
                _model = _extract_str(caps.get("model") or caps.get("default_model"))
            except Exception:
                _model = None

            provider_capabilities = {
                "supports_native_tools": bool(caps.get("supports_native_tools", False)),
                "provider_family": caps.get("provider_family") or "default",
                "model": _model,
                "provider_name": _pname or "",
            }
        except Exception:
            provider_capabilities = {}

        messages = builder.build_prompt(
            role_name="strategic",
            active_skills=[],
            task_description=replan_prompt,
            tools=tools_list,
            conversation=state.get("history", []),
            max_tokens=2000,
            provider_capabilities=provider_capabilities,
            model_tier=state.get("model_tier"),  # S1-B
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
            call_model(messages, stream=False, format_json=False)
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
            await asyncio.sleep(0.2)
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
                new_steps = json.loads(json_match.group())
                logger.info(f"replan_node: generated {len(new_steps)} new steps")
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
            else:
                new_plan = new_steps

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
                "current_step": current_step,  # Start from first new step
                "execution_waves": new_waves,  # WR-6 fix: include recomputed waves
                "replan_required": None,
                "action_failed": False,
                "replan_attempts": replan_attempts,
                "total_recovery_attempts": total_recovery_attempts,
                "last_plan_hash": _new_hash,
                # HR-13 fix: use system role with [internal] prefix to prevent
                # LLM from interpreting this as a user instruction
                "history": [
                    {
                        "role": "system",
                        "content": f"[internal] Replan: Split '{failed_step_desc}' into {len(new_steps)} smaller steps.",
                    }
                ],
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
                # HR-13 fix: use system role with [internal] prefix
                "history": [
                    {
                        "role": "system",
                        "content": "[internal] Replan failed: Could not generate smaller steps.",
                    }
                ],
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
