import logging
from pathlib import Path
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.memory.distiller import distill_context

logger = logging.getLogger(__name__)


async def memory_update_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Memory Update Layer: Persists distilled context to TASK_STATE.md.
    """
    logger.info("=== memory_update_node START ===")
    config.get("configurable", {}).get("orchestrator")  # Available for future use
    try:
        history_len = len(state.get("history", []))
        working_dir = state.get("working_dir", "unknown")
        logger.info(
            f"memory_update_node: distilling {history_len} messages from {working_dir}"
        )
        # Trigger distillation to sync TASK_STATE.md
        distill_context(state["history"], working_dir=Path(state["working_dir"]))
        logger.info("memory_update_node: distillation complete")
    except Exception as e:
        logger.error(f"memory_update_node: distillation failed: {e}")
    return {}
