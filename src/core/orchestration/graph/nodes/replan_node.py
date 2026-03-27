import logging
from typing import Dict, Any, Optional, List

from src.core.orchestration.graph.state import AgentState
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator

logger = logging.getLogger(__name__)


async def replan_node(state: AgentState, config: Any) -> Dict[str, Any]:
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

    orchestrator = _resolve_orchestrator(state, config)
    if orchestrator is None:
        logger.error("replan_node: orchestrator is None")
        return {
            "replan_required": None,
            "action_failed": False,
            "replan_attempts": replan_attempts,
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

        provider_capabilities = {}
        if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
            provider_capabilities = orchestrator.get_provider_capabilities()

        messages = builder.build_prompt(
            role_name="strategic",
            active_skills=[],
            task_description=replan_prompt,
            tools=tools_list,
            conversation=state.get("history", []),
            max_tokens=2000,
            provider_capabilities=provider_capabilities,
        )

        resp = await call_model(messages, stream=False, format_json=False)

        content = ""
        if isinstance(resp, dict):
            if resp.get("choices"):
                ch = resp["choices"][0].get("message")
                if isinstance(ch, dict):
                    content = ch.get("content") or ""
            elif resp.get("message"):
                content = resp.get("message", {}).get("content", "")

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

            return {
                "current_plan": new_plan,
                "current_step": current_step,  # Start from first new step
                "execution_waves": new_waves,  # WR-6 fix: include recomputed waves
                "replan_required": None,
                "action_failed": False,
                "replan_attempts": replan_attempts,
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
            "errors": [f"replan failed: {e}"],
        }
