import asyncio
import logging
import re
from pathlib import Path
from typing import Dict, Any, List
from concurrent.futures import ThreadPoolExecutor

from src.core.orchestration.graph.state import AgentState

# Hoisted to module level so tests can patch
# src.core.orchestration.graph.nodes.memory_update_node.distill_context
try:
    from src.core.memory.distiller import distill_context, compact_messages_to_prose
except ImportError:
    distill_context = None  # type: ignore[assignment]
    compact_messages_to_prose = None  # type: ignore[assignment]

from src.core.memory.advanced_features import (
    TrajectoryLogger,
    DreamConsolidator,
    RefactoringAgent,
    ReviewAgent,
    SkillLearner,
)

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)


async def memory_update_node(state: AgentState, config: Any) -> Dict[str, Any]:
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
            logger.info("memory_update_node: token budget threshold reached — forcing compact")
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

                summary = compact_messages_to_prose(history, working_dir=workdir_path)

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
                distilled = distill_context(state["history"], working_dir=workdir_path)
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

    async def run_dream_consolidation():
        try:
            consolidator = DreamConsolidator(str(workdir_path))
            result = consolidator.consolidate_memories()
            logger.info(
                f"memory_update_node: consolidation complete: {result.get('patterns', [])}"
            )
        except Exception as e:
            logger.warning(f"memory_update_node: consolidation failed: {e}")

    async def run_review_agent():
        if not (task_success and history):
            return
        try:
            patch = _extract_patch_from_history(history)
            if not patch:
                return

            review_agent = ReviewAgent(str(workdir_path))
            # Always inside asyncio.gather so a running loop always exists (NEW-13)
            loop = asyncio.get_running_loop()
            review_result = await loop.run_in_executor(
                _executor, review_agent.review_patch, patch
            )
            logger.info(
                f"memory_update_node: patch review complete: {review_result.get('overall', 'unknown')}"
            )

            # Save review to .agent-context/last_review.json
            review_agent.save_review(review_result)
        except Exception as e:
            logger.warning(f"memory_update_node: patch review failed: {e}")

    async def run_refactoring_agent():
        if not (task_success and history):
            return
        try:
            modified_files = _extract_modified_files(history)
            if not modified_files:
                return
            refactoring_agent = RefactoringAgent(str(workdir_path))

            async def analyze_file(file_path: str):
                if not file_path.endswith(".py"):
                    return []
                try:
                    # Always inside asyncio.gather so a running loop always exists (NEW-13)
                    loop = asyncio.get_running_loop()
                    smells = await loop.run_in_executor(
                        _executor, refactoring_agent.detect_code_smells, file_path
                    )
                    if smells:
                        logger.info(
                            f"memory_update_node: found {len(smells)} code smells in {file_path}"
                        )
                    return smells
                except Exception as e:
                    logger.warning(
                        f"memory_update_node: refactoring failed for {file_path}: {e}"
                    )
                    return []

            all_smells = []
            for smells in await asyncio.gather(
                *[analyze_file(f) for f in modified_files]
            ):
                all_smells.extend(smells)

            # Save smells to .agent-context/code_smells.json
            if all_smells:
                refactoring_agent.save_smells(all_smells)
        except Exception as e:
            logger.warning(f"memory_update_node: refactoring analysis failed: {e}")

    async def run_skill_learner():
        if not (task_success and task and len(tool_sequence) >= 2):
            return
        try:
            skill_learner = SkillLearner(str(workdir_path))
            existing_skills = skill_learner.list_skills()

            skill_name = re.sub(r"[^a-z0-9_]", "_", task[:40].lower()).strip("_")

            if skill_name and skill_name not in existing_skills:
                tool_names = [t.get("content", "")[:60] for t in tool_sequence[:5]]
                skill_learner.create_skill(
                    name=skill_name,
                    description=f"Auto-learned from successful task: {task[:120]}",
                    patterns=[f"Use {t}" for t in tool_names if t],
                    examples=[
                        {
                            "task": task[:200],
                            "solution": str(current_plan)[:400],
                        }
                    ],
                )
                logger.info(f"memory_update_node: created skill '{skill_name}'")
        except Exception as e:
            logger.warning(f"memory_update_node: skill learning failed: {e}")

    # Use return_exceptions=True so all tasks run even if some fail (H14 fix)
    results = await asyncio.gather(
        run_trajectory_logging(),
        run_dream_consolidation(),
        run_review_agent(),
        run_refactoring_agent(),
        run_skill_learner(),
        return_exceptions=True,
    )
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.warning(f"memory_update_node: parallel task {i} failed: {res}")

    logger.info("=== memory_update_node END ===")
    # HR-2 / ME-3 fix: return updated history and distilled summary via return dict.
    # Also clear _force_compact flag so it does not persist to the next turn.
    result: Dict[str, Any] = {"_force_compact": False}
    if _distilled_summary:
        result["analysis_summary"] = _distilled_summary
    if _updated_history is not None:
        result["history"] = _updated_history
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


def _extract_modified_files(history: List[Dict]) -> List[str]:
    """Extract list of modified files from history."""
    files = []
    for item in history:
        if isinstance(item, dict):
            content = str(item.get("content", ""))
            if ".py" in content or ".js" in content or ".ts" in content:
                paths = re.findall(
                    r"(?:[\w\-/]+\.(?:py|js|ts|jsx|tsx|md|txt))", content
                )
                files.extend(paths)
    return list(set(files))[:10]
