from langchain_core.runnables import RunnableConfig
import asyncio
import atexit
import logging
from pathlib import Path
from typing import Dict, Any, List
from concurrent.futures import ThreadPoolExecutor

from src.core.orchestration.graph.state import StateLike, replace_state_list

# Hoisted to module level so tests can patch
# src.core.orchestration.graph.nodes.memory_update_node.distill_context
try:
    from src.core.memory.distiller import distill_context, compact_messages_to_prose
except ImportError as _distiller_err:
    distill_context = None  # type: ignore[assignment]
    compact_messages_to_prose = None  # type: ignore[assignment]
    import logging as _log
    _log.getLogger(__name__).warning(
        "memory_update_node: distiller unavailable (%s) — context compaction disabled",
        _distiller_err,
    )

from src.core.memory.advanced_features import TrajectoryLogger

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)
# LOW-9 fix: register a shutdown hook so the executor's worker threads are
# joined when the interpreter exits, preventing ResourceWarning and ensuring
# any in-flight futures complete before the process terminates.
atexit.register(_executor.shutdown, wait=True)


async def memory_update_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """
    Memory Update Layer: Persists distilled context and triggers advanced memory features.
    Memory operations are parallelized for performance.
    Delegations can be spawned for LLM-heavy operations via state["delegations"].
    """
    logger.info("=== memory_update_node START ===")

    working_dir = state.get("working_dir", "unknown")
    workdir_path = Path(working_dir)

    history_len = len(state.get("history", []))
    logger.info(
        f"memory_update_node: processing {history_len} messages from {working_dir}"
    )

    evaluation_result = state.get("evaluation_result")
    task = state.get("task", "")
    current_plan = state.get("current_plan", [])
    history = state.get("history", [])
    tool_sequence = _extract_tool_sequence(history)
    session_id = state.get("session_id")
    task_success = evaluation_result == "complete" if evaluation_result else False

    # HR-6: Check token budget BEFORE deciding whether to distill/compact.
    # If the monitor reports "compact" (>85% usage), force a compaction regardless of
    # the _should_distill flag, and record the compaction turn to honour the 5-turn cooldown.
    _budget_forced_compact = False
    try:
        from src.core.orchestration.token_budget import get_token_budget_monitor

        _monitor = get_token_budget_monitor()
        if _monitor.check_budget(state) == "compact":
            _budget_forced_compact = True
            _monitor.check_and_prepare_compaction(session_id or "default")
            logger.info(
                "memory_update_node: token budget threshold reached — forcing compact"
            )
    except Exception as _budget_err:
        logger.debug(f"memory_update_node: token budget check skipped: {_budget_err}")

    should_distill = state.get("_should_distill", True)
    force_compact = state.get("_force_compact", False) or _budget_forced_compact

    # Track whether we need to return an updated history via the return dict
    # (LangGraph nodes must NOT mutate state in-place; mutations go in the return dict).
    _updated_history = None
    _distilled_summary: str = ""

    if should_distill:
        try:
            history = state.get("history", [])

            if force_compact:
                logger.info(
                    f"memory_update_node: FORCE COMPACT "
                    f"(history has {len(history)} messages)"
                )

                if compact_messages_to_prose is not None:
                    summary = compact_messages_to_prose(
                        history, working_dir=workdir_path
                    )
                else:
                    summary = "Context compacted."

                essential = [
                    {"role": "system", "content": "Session Summary:\n" + summary},
                    {"role": "user", "content": state.get("task", "")},
                ]

                # CF-1 / HR-2 fix: return updated history via return dict, not in-place.
                _updated_history = essential

                logger.info(
                    f"memory_update_node: compact complete "
                    f"(reduced to {len(essential)} messages)"
                )
            else:
                # HR-2 fix: capture distill_context return value and apply compacted
                # history to state so the context window is actually reduced when the
                # 50-message threshold triggers inside distill_context.
                if distill_context is not None:
                    # Use .get to avoid TypedDict non-required key access errors
                    distilled = distill_context(
                        state.get("history", []), working_dir=workdir_path
                    )
                else:
                    distilled = None
                compacted = distilled.get("_compacted_history") if distilled else None
                if compacted:
                    _updated_history = compacted
                    logger.info(
                        f"memory_update_node: history compacted by distill_context "
                        f"({len(compacted)} messages remain)"
                    )
                # ME-3 fix: feed the distilled current_state back into analysis_summary
                # so the next perception turn sees an up-to-date context summary rather
                # than the stale value from several turns ago.
                if distilled and distilled.get("current_state"):
                    _distilled_summary = distilled["current_state"]
                    logger.info(
                        "memory_update_node: updating analysis_summary from distilled state"
                    )

            logger.info("memory_update_node: distillation complete")
        except Exception as e:
            logger.error(f"memory_update_node: distillation failed: {e}")
    else:
        logger.info("memory_update_node: skipping distillation (continuing execution)")

    async def run_trajectory_logging():
        if not (task_success and task):
            return
        try:
            trajectory_logger = TrajectoryLogger(str(workdir_path))
            trajectory_logger.log_run(
                task=task,
                plan=str(current_plan),
                tool_sequence=tool_sequence,
                patch=_extract_patch_from_history(history),
                tests="",
                success=task_success,
                session_id=session_id or "",
            )
            logger.info("memory_update_node: trajectory logged")
        except Exception as e:
            logger.warning(f"memory_update_node: trajectory logging failed: {e}")

    advanced_tasks = [run_trajectory_logging()]

    # Use return_exceptions=True so all tasks run even if some fail (H14 fix)
    results = await asyncio.gather(
        *advanced_tasks,
        return_exceptions=True,
    )
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.warning(f"memory_update_node: parallel task {i} failed: {res}")

    logger.info("=== memory_update_node END ===")
    # HR-2 / ME-3 fix: return updated history and distilled summary via return dict.
    # Also clear _force_compact flag so it does not persist to the next turn.
    # CF-1 fix: clear errors so transient pipeline errors (e.g. context_overflow) do
    # not leak into the next outer-loop round and cause mis-routing.
    result: Dict[str, Any] = {"_force_compact": False, "errors": []}
    if _distilled_summary:
        result["analysis_summary"] = _distilled_summary
    if _updated_history is not None:
        result["history"] = replace_state_list(_updated_history)
    return result


def _extract_tool_sequence(history: List[Dict]) -> List[Dict]:
    """Extract tool calls from history."""
    tools = []
    for item in history:
        if isinstance(item, dict):
            role = item.get("role")
            content = item.get("content", "")
            if role == "tool" and content:
                tools.append({"content": content[:500]})
    return tools


def _extract_patch_from_history(history: List[Dict]) -> str:
    """Extract patch from tool call history."""
    for item in reversed(history):
        if isinstance(item, dict):
            content = item.get("content", "")
            if "diff" in content.lower() or "patch" in content.lower():
                return content
            if isinstance(content, str) and (
                "file" in content.lower() or "edited" in content.lower()
            ):
                return content
    return ""

