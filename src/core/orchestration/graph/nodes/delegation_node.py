import asyncio
import logging
from typing import Dict, Any, Optional, Tuple

from src.core.orchestration.graph.state import AgentState
from src.tools.subagent_tools import delegate_task_async

logger = logging.getLogger(__name__)


async def delegation_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Delegation Layer: Spawns subagents for independent tasks that can run in parallel.

    This node enables:
    - Background memory operations (consolidation, skill learning)
    - Parallel code analysis
    - Independent verification tasks
    - Async subagent execution without blocking main pipeline

    Subtasks are specified in state["delegations"] as a list of:
        {
            "role": "researcher|coder|reviewer",
            "task": "description of subtask",
            "result_key": "key to store result under"
        }
    """
    logger.info("=== delegation_node START ===")

    delegations = state.get("delegations", [])
    if not delegations:
        logger.info("delegation_node: no delegations to process")
        return {}

    working_dir = state.get("working_dir", "")
    results: Dict[str, Any] = {"delegation_results": {}}

    async def run_delegation(
        delegation: Dict[str, Any], index: int
    ) -> Optional[Tuple[str, Any]]:
        role = delegation.get("role", "researcher")
        task = delegation.get("task", "")
        result_key = delegation.get("result_key") or f"delegation_{index}"

        if not task:
            return None

        logger.info(f"delegation_node: spawning {role} subagent for: {task[:50]}...")

        try:
            result = await delegate_task_async(
                role=role,
                subtask_description=task,
                working_dir=working_dir,
            )
            logger.info(f"delegation_node: {result_key} completed")
            return (result_key, {"status": "completed", "result": result})
        except Exception as e:
            logger.error(f"delegation_node: delegation failed: {e}")
            return (result_key, {"status": "error", "error": str(e)})

    if len(delegations) == 1:
        result = await run_delegation(delegations[0], 0)
        if result is not None:
            result_key, value = result
            results["delegation_results"][result_key] = value
    else:
        tasks = [run_delegation(d, i) for i, d in enumerate(delegations)]
        delegation_results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in delegation_results:
            if isinstance(result, Exception):
                logger.error(f"delegation_node: exception in gather: {result}")
                continue
            if result is None:
                continue
            if not isinstance(result, tuple):
                logger.error(f"delegation_node: unexpected result type {type(result)}, skipping")
                continue
            key, value = result
            results["delegation_results"][key] = value

    logger.info("=== delegation_node END ===")
    return results


def create_delegation(
    role: str,
    task: str,
    result_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a delegation payload for the delegation node.

    Usage in other nodes:
        state["delegations"] = [
            create_delegation("researcher", "Analyze the codebase structure"),
            create_delegation("reviewer", "Review the recent changes"),
        ]
    """
    return {
        "role": role,
        "task": task,
        "result_key": result_key,
    }
