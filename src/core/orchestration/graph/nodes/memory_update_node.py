import asyncio
import logging
import re
from pathlib import Path
from typing import Dict, Any, List
from concurrent.futures import ThreadPoolExecutor

from src.core.orchestration.graph.state import AgentState
from src.core.memory.distiller import distill_context
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

    Memory operations are parallelized for performance:
    - Phase 1 (sequential): distill_context - required before delegations
    - Phase 2 (parallel): Trajectory logging, consolidation, review, refactoring

    Delegations can be spawned for LLM-heavy operations via state["delegations"].

    Memory writes go to:
    - .agent-context/TASK_STATE.md (distill_context)
    - .agent-context/trajectories/ (TrajectoryLogger)
    - .agent-context/consolidated/ (DreamConsolidator)
    - .agent-context/code_smells.json (RefactoringAgent)
    - .agent-context/last_review.json (ReviewAgent)
    - agent-brain/skills/ (SkillLearner)
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

    try:
        distill_context(state["history"], working_dir=workdir_path)
        logger.info("memory_update_node: distillation complete")
    except Exception as e:
        logger.error(f"memory_update_node: distillation failed: {e}")

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
    return {}


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
